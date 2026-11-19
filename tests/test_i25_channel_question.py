"""#25 D1: `channel question --id Q`, the keyed read of one open question's full body (docs/channel-contract.md §3.2)."""
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
BODY = "Which element did you put the sticker on?\nSecond line with the detail only the full body has."


class ChannelQuestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.other_home = base / "other"
        self.key = base / "desk.key"
        self.other_key = base / "other.key"
        for path, data in ((self.key, "k" * 40), (self.other_key, "o" * 40)):
            path.write_text(data + "\n")
            path.chmod(0o600)
        for home in (self.home, self.other_home):
            with Store(home) as store:
                store.initialize("Rowan", "maintainer")
        write_humans(self.home, {"default_human": "david", "senders": ["david"], "channel_prefixes": ["lookout:"],
                                 "operators": {"senders": ["codex-desktop"], "source_prefixes": []}})
        self.env = {k: v for k, v in os.environ.items() if k != "SYNTHETICS_SESSION_RUN"}
        self.env["PYTHONPATH"] = str(ROOT)
        out = self.cli(self.home, "channel", "register", "lookout-desk", "--source-prefix", "lookout:",
                       "--key-file", str(self.key))
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.cli(self.other_home, "channel", "register", "other", "--source-prefix", "other:",
                       "--key-file", str(self.other_key))
        self.assertEqual(out.returncode, 0, out.stderr)
        with Store(self.home) as store:
            self.work = store.work_put(title="needs David", key="w1")
            self.question = store.question_ask("david", BODY, work_id=self.work["id"], key="q1")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, home, *args, env=None):
        return subprocess.run([sys.executable, "-m", "synthetics", "--home", str(home)] + list(args),
                              capture_output=True, text=True, env=env or self.env, timeout=60)

    def ask(self, qid, key=None, home=None, env=None):
        out = self.cli(home or self.home, "channel", "question", "--key-file", str(key or self.key), "--id", qid,
                       env=env)
        rows = [json.loads(text) for text in out.stdout.splitlines() if text.strip()]
        self.assertEqual(len(rows), 1, out.stdout + out.stderr)
        return out.returncode, rows[0]

    def audit(self):
        with Store(self.home) as store:
            return [(r[0], r[1], r[2]) for r in store.db.execute(
                "SELECT action, record_id, data FROM audit WHERE action='channel.question_read' ORDER BY seq")]

    def test_open_question_to_default_human_returns_full_body(self):
        code, row = self.ask(self.question["id"])
        self.assertEqual(code, 0)
        self.assertEqual((row["v"], row["question_id"], row["to"], row["body"], row["error"]),
                         (1, self.question["id"], "david", BODY, None))
        self.assertEqual(row["work_ids"], [self.work["id"]])
        audit = self.audit()
        self.assertEqual(len(audit), 1)
        self.assertEqual(json.loads(audit[0][2]), {"channel": "lookout-desk", "result": "returned"})
        self.assertNotIn("sticker", audit[0][2])

    def test_read_only(self):
        with Store(self.home) as store:
            before = store._get("questions", self.question["id"])
            events = store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.ask(self.question["id"])
        with Store(self.home) as store:
            self.assertEqual(store._get("questions", self.question["id"]), before)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
            self.assertEqual(store._get("works", self.work["id"])["status"], "waiting")

    def test_every_refusal_is_the_same_error(self):
        with Store(self.home) as store:
            to_peer = store.question_ask("employee-vesper", "peer question", key="q2")
            answered = store.question_ask("david", "answered one", key="q3")
            relayed = store.question_ask("david", "relayed one", key="q4")
            store.feed("david", "yes", source="manual", external_id="a3", reply_to=answered["id"])
            store.db.execute("UPDATE questions SET data=json_set(data,'$.to_id','codex-desktop') WHERE id=?",
                             (relayed["id"],))
            store.db.commit()
        refusals = []
        for qid in ("question-doesnotexist", to_peer["id"], answered["id"], relayed["id"]):
            code, row = self.ask(qid)
            refusals.append((code, row["result"], row["error"]["code"], row["error"]["message"]))
            self.assertNotIn("body", row)
        self.assertEqual(len(set(refusals)), 1, refusals)
        self.assertEqual(refusals[0][:3], (0, "rejected", "unknown_message"))
        results = [json.loads(data)["result"] for _a, _r, data in self.audit()]
        self.assertEqual(results, ["refused"] * 4)

    def test_no_humans_file_refuses(self):
        os.unlink(self.home / "humans.json")
        code, row = self.ask(self.question["id"])
        self.assertEqual(row["error"]["code"], "unknown_message")

    def test_key_of_another_home_is_auth_failed(self):
        code, row = self.ask(self.question["id"], key=self.other_key)
        self.assertEqual((code, row["error"]["code"]), (2, "auth_failed"))
        self.assertEqual(self.audit(), [])

    def test_refused_in_session(self):
        code, row = self.ask(self.question["id"], env=dict(self.env, SYNTHETICS_SESSION_RUN="run-x"))
        self.assertEqual((code, row["error"]["code"]), (2, "in_session"))
        with Store(self.home) as store:
            store.run_id = "run-x"
            with self.assertRaises(Exception):
                store.channel_question(self.key.read_bytes(), self.question["id"])
        self.assertEqual(self.audit(), [])


if __name__ == "__main__":
    unittest.main()
