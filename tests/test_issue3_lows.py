"""Issue #3 follow-ups (Codex's lows on the #3 review): (a) intent field types are refused as bad_intent/bad_claims
before commit, (b) free-text lesson evidence shows in the generated view, (c) a failing log.jsonl rebuild during a
failed redact does not hide the original error."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics.projects import ProjectError, ProjectStore


def src(text):
    return {"kind": "source", "bytes": text,
            "body": {"origin": {"role": "human", "auth": None, "thread": "t1", "sender": "david"},
                     "capture": {"mode": "promotion"}, "quote_source": "event"}}


class Issue3LowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProjectStore(self.root / "p" / "memory.sqlite3", "p")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, batch):
        return self.store.append(batch, author="vesper", run="run-x")

    def intent(self, sid, **over):
        body = {"title": "t", "claims": [{"cid": "c1", "text": "x"}], "sources": [sid]}
        body.update(over)
        return {"kind": "intent", "body": body}

    def test_a_intent_wrong_types_refused_before_commit(self):
        sid = self.add([src("David said x")])["ids"][0]
        cases = [
            ("bad_intent", {"title": 5}),
            ("bad_intent", {"scope_note": ["x"]}),
            ("bad_claims", {"claims": "c1"}),
            ("bad_claims", {"claims": {"cid": "c1", "text": "x"}}),
            ("bad_claims", {"claims": ["c1"]}),
            ("bad_claims", {"claims": [{"cid": "c1", "text": 5}]}),
            ("bad_claims", {"claims": [{"cid": 1, "text": "x"}]}),
        ]
        for code, over in cases:
            with self.subTest(over=over):
                head = self.store.head_seq()
                with self.assertRaises(ProjectError) as cm:
                    self.add([self.intent(sid, **over)])
                self.assertEqual(cm.exception.code, code)
                self.assertEqual(self.store.head_seq(), head, "a refused batch must commit nothing")
        self.add([self.intent(sid)])  # the well-formed intent still lands
        self.add([self.intent(sid, title=None)])  # absent/None title stays accepted as before

    def test_b_free_text_evidence_shown_in_view(self):
        body = {"statement": "s", "applies_when": "a", "uncertainty": "none known",
                "evidence": "reviews/x/run1.txt line 4"}
        self.add([{"kind": "lesson", "body": body}])
        view = self.store.view_path.read_text(encoding="utf-8")
        self.assertIn("evidence: reviews/x/run1.txt line 4", view)

    def test_c_failed_rebuild_does_not_mask_the_redact_error(self):
        sid = self.add([src("SECRET-TEXT-xyz")])["ids"][0]

        def append_fails(*a, **k):
            raise RuntimeError("original append error")

        def export_fails(store, rebuild=False):
            raise OSError("disk full")

        with mock.patch.object(ProjectStore, "append", append_fails), \
                mock.patch.object(ProjectStore, "export", export_fails):
            with self.assertRaises(ProjectError) as cm:
                self.store.redact(sid, reason="test", by="owner")
        self.assertEqual(cm.exception.code, "redact_rebuild_failed")
        self.assertIn("original append error", str(cm.exception))
        self.assertIn("disk full", str(cm.exception))
        self.assertIsInstance(cm.exception.__cause__, RuntimeError)

    def test_c_original_error_kept_when_rebuild_succeeds(self):
        sid = self.add([src("SECRET-TEXT-xyz")])["ids"][0]
        with self.assertRaises(ProjectError) as cm:
            self.store.redact("pm-doesnotexist", reason="test", by="owner")
        self.assertNotEqual(cm.exception.code, "redact_rebuild_failed")
        self.assertIn("SECRET-TEXT-xyz", self.store.export_path.read_text(encoding="utf-8"))
        self.store.redact(sid, reason="test", by="owner")
        self.assertNotIn("SECRET-TEXT-xyz", self.store.export_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
