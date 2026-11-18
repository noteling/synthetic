"""M21 phase 3: the generated read-only view (view.md, `project view`)."""
import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics.cli import main
from synthetics.projects import ProjectStore
from synthetics.projectview import render
from synthetics.store import SESSION_ENV, Store

HOME_ENV = "SYNTHETICS_HOME"


def src(text, thread="t1"):
    return {"kind": "source", "bytes": text,
            "body": {"origin": {"role": "human", "auth": {"channel": "fieldpost"}, "thread": thread,
                                "sender": "david"}, "capture": {"mode": "promotion"}, "quote_source": "event"}}


def intent(sources, claims, title):
    return {"kind": "intent", "body": {"act": "request", "title": title, "sources": sources,
                                        "claims": [{"cid": c, "text": t} for c, t in claims]}}


def lesson(title, statement, evidence):
    return {"kind": "lesson", "body": {"title": title, "statement": statement, "applies_when": "always",
                                        "uncertainty": "none known",
                                        "evidence": [{"ref": evidence, "level": "observed"}]}}


class ProjectViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProjectStore(self.root / "synthetics" / "memory.sqlite3", "synthetics")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, batch, **kw):
        return self.store.append(batch, author="vesper", run="run-x", **kw)

    def read_view(self):
        return self.store.view_path.read_text(encoding="utf-8")

    def seed(self):
        s1 = self.add([src("David: use Telegram for owner reports")])["ids"][0]
        r1 = self.add([intent([s1], [("c1", "report owner-level"), ("c2", "use the native memory")], "reports")])
        r1 = r1["ids"][0]
        return s1, r1

    def test_view_written_after_each_append_and_equals_render(self):
        s1, r1 = self.seed()
        self.assertTrue(self.store.view_path.is_file())
        self.assertEqual(stat.S_IMODE(self.store.view_path.stat().st_mode), 0o600)
        text = self.read_view()
        self.assertEqual(text, self.store.view())
        self.assertEqual(text, render(self.store.entries(), "synthetics", self.store.source_bytes))
        self.assertIn(f"as of seq {self.store.head_seq()}", text)
        self.assertIn("c2: use the native memory", text)
        self.assertIn("David: use Telegram", text)
        self.assertEqual(self.store.view(), self.store.view())  # deterministic: no clock, no host data

    def test_revision_moves_old_claim_to_history_and_as_of_matches_earlier_view(self):
        s1, r1 = self.seed()
        before_seq, before = self.store.head_seq(), self.read_view()
        s2 = self.add([src("David: project memory, not native memory")])["ids"][0]
        r2 = self.add([intent([s2], [("c1", "use project memory")], "memory")])["ids"][0]
        self.add([{"kind": "relate", "body": {"rel": "revises", "from": f"{r2}#c1", "target": f"{r1}#c2",
                                               "expected_head": f"{r1}#c2"}}])
        now = self.read_view()
        standing, history = now.split("## History", 1)
        self.assertNotIn("c2: use the native memory", standing)
        self.assertIn(f"c2 now reads ({r2}#c1): use project memory", standing)
        self.assertIn(f"claim {r1}#c2 superseded by {r2}#c1: use the native memory", history)
        self.assertEqual(self.store.view(as_of=before_seq), before)

    def test_lessons_grouped_by_status(self):
        s1, _ = self.seed()
        a = self.add([lesson("keep sums", "freeze by sums", s1)])["ids"][0]
        b = self.add([lesson("old rule", "chmod the tree", s1)])["ids"][0]
        self.add([{"kind": "status", "body": {"target": a, "to": "accepted", "reason": "used twice"}},
                  {"kind": "status", "body": {"target": b, "to": "retired", "reason": "wrong"}}])
        text = self.read_view()
        acc = text.split("### Accepted (1)", 1)[1].split("### Proposed", 1)[0]
        self.assertIn("keep sums", acc)
        self.assertIn("applies when: always", acc)
        self.assertIn(f"evidence: {s1}", acc)
        self.assertIn(f"lesson {b} retired: old rule", text.split("## History", 1)[1])
        self.assertNotIn("old rule", text.split("## History", 1)[0])

    def test_redacted_bytes_never_in_view_including_as_of(self):
        s1, r1 = self.seed()
        seq = self.store.head_seq()
        self.assertIn("David: use Telegram", self.store.view(as_of=seq))
        self.store.redact(s1, reason="private", by="op")
        now = self.read_view()
        self.assertNotIn("David: use Telegram", now)
        self.assertIn(f"intent {r1} withdrawn: reports", now.split("## History", 1)[1])  # every source redacted
        as_of = self.store.view(as_of=seq)
        self.assertNotIn("David: use Telegram", as_of)
        self.assertIn("[redacted]", as_of)

    def test_view_write_failure_never_fails_append_and_recovers(self):
        self.seed()
        self.store.view_path.unlink()
        self.store.view_path.mkdir()  # os.replace onto a directory fails
        res = self.add([src("still committed")])
        self.assertEqual(self.store.head_seq(), res["seq_to"])
        self.assertTrue(self.store.view_path.is_dir())
        self.assertEqual(list(self.store.view_path.parent.glob(".view.md.*")), [])  # temp file removed
        shutil.rmtree(self.store.view_path)
        self.add([src("next write")])
        self.assertEqual(self.read_view(), self.store.view())

    def test_import_gives_identical_view(self):
        self.seed()
        copy = self.root / "copy.jsonl"
        copy.write_bytes(self.store.export_path.read_bytes())
        restored = ProjectStore.import_jsonl(copy, self.root / "restored" / "memory.sqlite3", "synthetics")
        try:
            self.assertEqual(restored.view_path.read_text(encoding="utf-8"), self.read_view())
        finally:
            restored.close()

    def test_view_has_no_side_effects_on_the_log(self):
        self.seed()
        head = self.store.head_seq()
        audit = self.store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        self.store.view()
        self.store.write_view()
        self.assertEqual(self.store.head_seq(), head)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], audit)


class ProjectViewCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "manager"
        with Store(self.home) as store:
            store.initialize("Codex", "manager")
            self.me = store.identity()["id"]
            self.run = store.run_begin()["id"]
        with ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics") as ps:
            s = ps.append([src("hello view")], author="op", operator=True)["ids"][0]
            ps.append([intent([s], [("c1", "say hello")], "greeting")], author="op", operator=True)
            self.view = ps.view()
            self.head = ps.head_seq()

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, session=False):
        out, err = io.StringIO(), io.StringIO()
        env = {k: v for k, v in os.environ.items() if k not in (SESSION_ENV, HOME_ENV)}
        if session:
            env.update({SESSION_ENV: self.run, HOME_ENV: str(self.home)})
        with mock.patch.dict(os.environ, env, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["--home", str(self.home)] + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_owner_prints_view_as_of_and_write(self):
        code, out, err = self.cli("project", "view", "synthetics")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, self.view)
        code, out, _ = self.cli("project", "view", "synthetics", "--as-of", "1")
        self.assertIn("as of seq 1", out)
        self.assertNotIn("say hello", out)
        dest = self.root / "v.md"
        code, out, err = self.cli("project", "view", "synthetics", "--out", str(dest))
        self.assertEqual(code, 0, err)
        self.assertEqual(dest.read_text(encoding="utf-8"), self.view)
        code, out, err = self.cli("project", "view", "synthetics", "--write")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["written"])

    def test_session_needs_read_grant(self):
        with ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics") as ps:
            ps.grant("employee-other", "read", by="op")
        code, out, err = self.cli("project", "view", "synthetics", session=True)
        self.assertNotEqual(code, 0)
        self.assertIn("No read grant", err)
        self.assertNotIn("say hello", out)
        with ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics") as ps:
            ps.grant(self.me, "read", by="op")
        code, out, err = self.cli("project", "view", "synthetics", session=True)
        self.assertEqual(code, 0, err)
        self.assertIn("say hello", out)


if __name__ == "__main__":
    unittest.main()
