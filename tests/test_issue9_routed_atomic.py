"""Issue #9: a routed answer that the feed rejects must not leave a question_routed stamp behind."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from synthetics.capture import write_humans
from synthetics.store import Store

ROOT = Path(__file__).resolve().parents[1]


def line(**fields):
    return dict({"v": 1, "kind": "message", "thread": None}, **fields)


class RoutedStampAtomicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.key = base / "desk.key"
        self.key.write_text("k" * 40 + "\n")
        self.key.chmod(0o600)
        with Store(self.home) as store:
            store.initialize("Vesper", "maintainer")
        self.env = {k: v for k, v in os.environ.items() if k not in ("SYNTHETICS_SESSION_RUN", "SYNTHETICS_HOME")}
        self.env["PYTHONPATH"] = str(ROOT)
        out = self.cli("channel", "register", "desk", "--source-prefix", "desk:", "--key-file", str(self.key),
                       "--route", "david")
        self.assertEqual(out.returncode, 0, out.stderr)
        write_humans(self.home, {"default_human": "david", "senders": ["david"],
                                 "operators": {"senders": ["codex-desktop", "secretary"], "source_prefixes": []}})
        with Store(self.home) as store:
            self.question = store.question_ask("david", "ok?", key="q9")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home)] + list(args),
                              input=stdin, capture_output=True, text=True, env=self.env, timeout=60)

    def inbound(self, *items):
        stdin = "".join(json.dumps(item) + "\n" for item in items)
        out = self.cli("channel", "inbound", "--key-file", str(self.key), stdin=stdin)
        return [json.loads(text) for text in out.stdout.splitlines() if text.strip()]

    def stored(self):
        with Store(self.home) as store:
            q = store._get("questions", self.question["id"])
            actions = [r[0] for r in store.db.execute("SELECT action FROM audit ORDER BY seq")]
        return q, actions

    def test_rejected_routed_line_leaves_no_stamp(self):
        # x1 is first used for a plain message; reusing it with a different body is a conflict.
        rows = self.inbound(line(external_id="x1", sender="codex-desktop", body="plain", source="desk:relay"))
        self.assertEqual(rows[0]["result"], "accepted", rows)
        rows = self.inbound(line(external_id="x1", sender="codex-desktop", body="David says yes",
                                 source="desk:relay", question_id=self.question["id"], routed=True))
        self.assertEqual((rows[0]["result"], rows[0]["error"]["code"]), ("rejected", "conflict"), rows)
        q, actions = self.stored()
        self.assertEqual((q["status"], q.get("to_id"), q.get("routed_by")), ("open", None, None))
        self.assertNotIn("question.routed", actions)

    def test_other_operator_can_route_after_a_rejected_line(self):
        self.inbound(line(external_id="x1", sender="codex-desktop", body="plain", source="desk:relay"))
        self.inbound(line(external_id="x1", sender="codex-desktop", body="David says yes",
                          source="desk:relay", question_id=self.question["id"], routed=True))
        rows = self.inbound(line(external_id="s1", sender="secretary", body="David says yes",
                                 source="desk:relay", question_id=self.question["id"], routed=True))
        self.assertEqual(rows[0]["result"], "accepted", rows)
        q, actions = self.stored()
        self.assertEqual((q["status"], q["to_id"], q["routed_by"]), ("answered", "secretary", "channel:desk"))
        self.assertEqual(actions.count("question.routed"), 1)

    def test_accepted_routed_line_still_stamps_and_duplicate_is_idempotent(self):
        rows = self.inbound(line(external_id="a1", sender="codex-desktop", body="yes", source="desk:relay",
                                 question_id=self.question["id"], routed=True))
        self.assertEqual(rows[0]["result"], "accepted", rows)
        q, _ = self.stored()
        self.assertEqual((q["to_id"], q["routed_by"]), ("codex-desktop", "channel:desk"))
        # Same line again: duplicate, no second stamp.
        rows = self.inbound(line(external_id="a1", sender="codex-desktop", body="yes", source="desk:relay",
                                 question_id=self.question["id"], routed=True))
        self.assertEqual(rows[0]["result"], "duplicate", rows)

    def test_store_feed_route_rolls_back_with_feed(self):
        with Store(self.home) as store:
            store.feed("codex-desktop", "plain", source="desk:relay", external_id="z1")
            with self.assertRaises(ValueError):
                store.feed("codex-desktop", "other", source="desk:relay", external_id="z1",
                           reply_to=self.question["id"], route="channel:desk")
            q = store._get("questions", self.question["id"])
        self.assertIsNone(q.get("to_id"))


if __name__ == "__main__":
    unittest.main()
