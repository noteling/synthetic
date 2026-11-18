"""Backlog 27: operator redaction of Board posts (design 824e58d1; Codex conditions B1-B5)."""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import board
from synthetics.cli import main
from synthetics.projects import ProjectError, ProjectStore
from synthetics.store import SESSION_ENV, Store

MARKER = "SECRET-MARKER-7f3a"


def env_without_session():
    env = dict(os.environ)
    env.pop(SESSION_ENV, None)
    return mock.patch.dict(os.environ, env, clear=True)


class BoardRedactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "vesper"
        self.store = Store(self.home)
        self.me = self.store.initialize("Vesper", "maintainer")["id"]
        self.rowan = Store(self.root / "rowan")
        self.rid = self.rowan.initialize("Rowan", "maintainer")["id"]
        with self.ps() as ps:
            ps.grant(self.me, "post", by="operator")
            ps.grant(self.rid, "post", by="operator")
        board.scan(self.store)  # first scan starts at head

    def tearDown(self):
        self.store.close()
        self.rowan.close()
        self.tmp.cleanup()

    def ps(self):
        return ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")

    def add(self, batch, author, **kw):
        with self.ps() as ps:
            return ps.append(batch, author=author, **kw)["ids"][0]

    def post(self, topic, author, key=None, **kw):
        text = "plain reply" if kw.get("reply_to") else "text " + MARKER  # replies carry no marker
        return self.add([{"kind": "post", "body": dict(topic=topic, body=text, **kw)}], author, key=key)

    def cli(self, *args, session=None):
        out, err = io.StringIO(), io.StringIO()
        with env_without_session():
            if session:
                os.environ[SESSION_ENV] = session
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["--home", str(self.home)] + list(args))
        return code, out.getvalue(), err.getvalue()

    def all_text(self, db_path):
        """Every text/blob value in every table of a SQLite file, joined."""
        con = sqlite3.connect(str(db_path))
        try:
            parts = []
            for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                for row in con.execute('SELECT * FROM "%s"' % table):
                    for v in row:
                        if isinstance(v, bytes):
                            v = v.decode("utf-8", "replace")
                        parts.append(str(v))
            return "\n".join(parts)
        finally:
            con.close()

    def test_redact_post_b1_b2_b3(self):
        root = self.post("root " + MARKER, self.rid, key="k-root", mentions=[self.me], refs=["work-x"])
        reply = self.post("re", self.me, reply_to=root)
        with self.ps() as ps:
            stored = ps.db.execute("SELECT body FROM entries WHERE id=?", (root,)).fetchone()[0]
            self.assertIn(MARKER, self.all_text(ps.path))
            out = ps.redact(root, reason="pasted secret", by="ops")
            # B1: hash over the exact canonical bytes stored in entries.body
            self.assertEqual(out["sha256"], hashlib.sha256(stored.encode("utf-8")).hexdigest())
            self.assertEqual(out["size"], len(stored.encode("utf-8")))
            self.assertEqual(out["kind"], "post")
            self.assertFalse(out["copies"]["removed"])
            self.assertIn("any home with read access may hold one", out["copies"]["note"])
            self.assertEqual(sorted(out["copies"]["read_access"]), sorted([self.me, self.rid]))
            st = ps.state()
            body = json.loads(ps.db.execute("SELECT body FROM entries WHERE id=?", (root,)).fetchone()[0])
            path, export = ps.path, ps.export_path
        self.assertEqual(body, {"topic": "", "body": "", "mentions": [], "refs": [], "reply_to": None,
                                "redacted": True})
        # B3: the reply keeps its parent; the root is a placeholder
        self.assertEqual(st["posts"][reply]["root"], root)
        self.assertEqual(st["posts"][root]["topic"], "(redacted)")
        self.assertTrue(st["posts"][root]["redacted"])
        self.assertEqual(st["posts"][root]["replies"], [reply])
        # B2: the marker is in no table (entries, keys.payload, state, audit, ...) nor the export
        self.assertNotIn(MARKER, self.all_text(path))
        self.assertNotIn(MARKER, Path(export).read_text(encoding="utf-8"))
        # key replay still works against the hashed payload
        with self.ps() as ps:
            again = ps.append([{"kind": "post", "body": dict(topic="root " + MARKER, body="text " + MARKER,
                                                             mentions=[self.me], refs=["work-x"])}],
                              author=self.rid, key="k-root")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["ids"], [root])

    def test_redacted_post_never_wakes_and_shows_placeholder(self):
        pid = self.post("ping", self.rid, mentions=[self.me])
        with self.ps() as ps:
            ps.redact(pid, reason="r", by="ops")
        self.assertEqual(board.scan(self.store), [])
        sec = board.section(self.store, self.me)
        self.assertNotIn(MARKER, json.dumps(sec))
        self.assertIn("(redacted)", json.dumps(sec))

    def test_b4_errors(self):
        pid = self.post("p", self.rid)
        with self.ps() as ps:
            ps.redact(pid, reason="r", by="ops")
            with self.assertRaises(ProjectError) as cm:
                ps.redact(pid, reason="r", by="ops")
            self.assertEqual(cm.exception.code, "already_redacted")
            ps.append([{"kind": "backlog", "body": {"work_id": "work-1", "title": "t", "status": "ready",
                                                    "origin": "self"}}], author=self.me)
            bid = [e["id"] for e in ps.entries() if e["kind"] == "backlog"][0]
            with self.assertRaises(ProjectError) as cm:
                ps.redact(bid, reason="r", by="ops")
            self.assertEqual(cm.exception.code, "unknown_target")
            with self.assertRaises(ProjectError) as cm:
                ps.redact("pm-nope", reason="r", by="ops")
            self.assertEqual(cm.exception.code, "unknown_target")
        pid2 = self.post("p2", self.rid)
        code, out, err = self.cli("project", "redact", "synthetics", pid2, "--reason", "x", "--by", "op",
                                  session="run-abc")
        self.assertNotEqual(code, 0)
        self.assertRegex(err, r"refused inside an employee session|actor_unverified|owner_only|not_delegated|run_inactive")  # M28
        code, out, err = self.cli("project", "redact", "synthetics", pid2, "--reason", "x", "--by", "op")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["redacted"], pid2)

    def test_b5_import_old_export_plus_tombstone(self):
        root = self.post("root", self.rid)
        reply = self.post("re", self.me, reply_to=root)
        with self.ps() as ps:
            old = Path(ps.export_path).read_text(encoding="utf-8")
            ps.redact(root, reason="r", by="ops")
            tomb = [line for line in Path(ps.export_path).read_text(encoding="utf-8").splitlines()
                    if '"kind":"redact"' in line]
        self.assertIn(MARKER, old)
        self.assertEqual(len(tomb), 1)
        jsonl = self.root / "old-plus-tombstone.jsonl"
        jsonl.write_text(old + tomb[0] + "\n", encoding="utf-8")
        imported = ProjectStore.import_jsonl(jsonl, self.root / "copy" / "memory.sqlite3", "synthetics")
        try:
            body = json.loads(imported.db.execute("SELECT body FROM entries WHERE id=?", (root,)).fetchone()[0])
            self.assertEqual(body["body"], "")
            self.assertTrue(body["redacted"])
            self.assertEqual(imported.state()["posts"][reply]["root"], root)
            path = imported.path
        finally:
            imported.close()
        self.assertNotIn(MARKER, self.all_text(path))

    def test_source_redaction_unchanged(self):
        with self.ps() as ps:
            sid = ps.append([{"kind": "source", "body": {"origin": {"role": "human"}}, "bytes": MARKER}],
                            author="operator:op", operator=True)["ids"][0]
            out = ps.redact(sid, reason="r", by="ops")
            self.assertEqual(out["sha256"], hashlib.sha256(MARKER.encode()).hexdigest())
            self.assertNotIn("copies", out)
            self.assertTrue(ps.state()["sources"][sid]["redacted"])
            self.assertEqual(ps.source_bytes(sid), b"")


if __name__ == "__main__":
    unittest.main()
