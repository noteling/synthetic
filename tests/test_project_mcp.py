"""M21 step 6 MCP tools: fenced, attributed appends; read grants; promote/skip/uncovered through the server."""
import tempfile
import unittest
from pathlib import Path

from synthetics.mcp import METHODS, MUTATIONS, Server
from synthetics.projects import ProjectError, ProjectStore
from synthetics.store import Store
from tests.humans_fixture import write_host_humans


def lesson(text="plistlib drops the managed marker"):
    return {"kind": "lesson", "body": {"statement": text, "lesson_kind": "discovery", "evidence": [],
                                        "applies_when": "service.py", "uncertainty": "none known"}}


class ProjectMcpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "vesper"
        self.store = Store(self.home)
        write_host_humans(self.store.home)
        self.me = self.store.initialize("Vesper", "maintainer")["id"]
        self.run = self.store.run_begin()["id"]
        self.bound = Store(self.home, run_id=self.run)
        self.server = Server(self.bound, run_id=self.run)

    def tearDown(self):
        self.bound.close()
        self.store.close()
        self.tmp.cleanup()

    def ps(self, project="synthetics"):
        return ProjectStore(self.root / "projects" / project / "memory.sqlite3", project)

    def test_tools_registered(self):
        for name in ("project_memory_append", "project_memory_get", "project_memory_search", "project_uncovered",
                     "project_skip", "project_promote"):
            self.assertIn(name, METHODS)
        self.assertIn("project_promote", MUTATIONS)
        self.assertNotIn("project_redact", METHODS)
        self.assertNotIn("project_bind", METHODS)

    def test_append_is_attributed_fenced_and_idempotent(self):
        out = self.server.call("project_memory_append", {"project": "synthetics", "entries": [lesson()], "key": "l1"})
        again = self.server.call("project_memory_append", {"project": "synthetics", "entries": [lesson()], "key": "l1"})
        self.assertEqual(out["ids"], again["ids"])
        with self.ps() as ps:
            e = ps.entries()
        self.assertEqual(len(e), 1)
        self.assertEqual((e[0]["author"], e[0]["run"]), (self.me, self.run))
        hits = self.server.call("project_memory_search", {"project": "synthetics", "query": "plistlib marker"})
        self.assertIn(out["ids"][0], str(hits))
        got = self.server.call("project_memory_get", {"project": "synthetics", "id": out["ids"][0]})
        self.assertEqual(got["record"]["kind"], "lesson")
        self.store.run_finish(self.run, "done")
        with self.assertRaises(ProjectError):
            self.server.call("project_memory_append", {"project": "synthetics", "entries": [lesson("x")],
                                                       "key": "l2"})

    def test_source_and_operator_kinds_refused(self):
        for kind in ("source", "redact", "binding"):
            with self.assertRaises(ProjectError):
                self.server.call("project_memory_append", {"project": "synthetics",
                                                           "entries": [{"kind": kind, "body": {}}], "key": kind})

    def test_read_grant_gates_get_and_search(self):
        out = self.server.call("project_memory_append", {"project": "synthetics", "entries": [lesson()], "key": "l"})
        with self.ps() as ps:
            ps.grant("employee-other", "write", by="op")
        with self.assertRaises(ProjectError) as cm:
            self.server.call("project_memory_get", {"project": "synthetics", "id": out["ids"][0]})
        self.assertEqual(cm.exception.code, "no_grant")
        with self.assertRaises(ProjectError):
            self.server.call("project_memory_search", {"project": "synthetics", "query": "plistlib"})
        with self.assertRaises(ProjectError):
            self.server.call("project_memory_append", {"project": "synthetics", "entries": [lesson("y")],
                                                       "key": "l3"})

    def test_promote_with_intent_and_considered_then_uncovered_and_skip(self):
        ev = self.store.feed("david", "Keep it simple. Telegram is output.", source="codex-desktop:abc",
                             thread="t")
        other = self.store.feed("david", "Unrelated note.", source="codex-desktop:abc", thread="t")
        intent = {"kind": "intent", "body": {"act": "preference", "title": "channels", "sources": ["$source"],
                                             "claims": [{"cid": "c1", "text": "Telegram is output to David."}]}}
        out = self.server.call("project_promote", {"project": "synthetics", "text": "Telegram is output.",
                                                   "event_id": ev["id"], "considered": "captured as intent",
                                                   "entries": [intent], "key": "p1"})
        self.assertEqual(len(out["ids"]), 3)
        got = self.server.call("project_memory_get", {"project": "synthetics", "id": out["ids"][2]})
        self.assertEqual(got["quote_source"], out["ids"][0])
        with self.assertRaises(ProjectError) as cm:
            self.server.call("project_promote", {"project": "synthetics", "text": "Telegram  is output.",
                                                 "event_id": ev["id"], "key": "p2"})
        self.assertEqual(cm.exception.code, "not_substring")
        rep = self.server.call("project_uncovered", {})
        private = [r["event_id"] if isinstance(r, dict) else r for r in rep["private"]]
        self.assertNotIn(ev["id"], str(private))
        self.assertIn(other["id"], str(private))
        self.server.call("project_skip", {"event_id": other["id"], "reason": "not project content"})
        self.assertNotIn(other["id"], str(self.server.call("project_uncovered", {})["private"]))

    def test_promote_file_needs_granted_root(self):
        granted = self.root / "granted"
        granted.mkdir()
        (granted / "doc.md").write_text("alpha beta gamma")
        (self.root / "outside.md").write_text("alpha beta gamma")
        self.store.access_grant(str(granted), "read", "docs", granted_by="david")
        out = self.server.call("project_promote", {"project": "synthetics", "text": "beta",
                                                   "file": str(granted / "doc.md"), "key": "f1"})
        self.assertEqual(len(out["ids"]), 1)
        with self.assertRaises(ProjectError):
            self.server.call("project_promote", {"project": "synthetics", "text": "beta",
                                                 "file": str(self.root / "outside.md"), "key": "f2"})

    def test_bad_project_name(self):
        with self.assertRaises(ValueError):
            self.server.call("project_memory_search", {"project": "../x", "query": "a"})


if __name__ == "__main__":
    unittest.main()
