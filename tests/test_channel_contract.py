"""M25 §1 channel contract v1 through the real CLI (design tests 1-2, C1 routed answers)."""
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


class ChannelContractCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.key = base / "desk.key"
        self.other_key = base / "other.key"
        for path, data in ((self.key, "k" * 40), (self.other_key, "o" * 40)):
            path.write_text(data + "\n")
            path.chmod(0o600)
        with Store(self.home) as store:
            store.initialize("Vesper", "maintainer")
        self.env = {k: v for k, v in os.environ.items() if k != "SYNTHETICS_SESSION_RUN"}
        self.env["PYTHONPATH"] = str(ROOT)
        out = self.cli("channel", "register", "desk", "--source-prefix", "desk:", "--key-file", str(self.key),
                       "--route", "david")
        self.assertEqual(out.returncode, 0, out.stderr)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, stdin=None, env=None):
        return subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home)] + list(args),
                              input=stdin, capture_output=True, text=True, env=env or self.env, timeout=60)

    def contract(self, command, *args, lines=None, key=None, env=None):
        stdin = "".join(json.dumps(item) + "\n" for item in lines) if lines is not None else None
        out = self.cli("channel", command, "--key-file", str(key or self.key), *args, stdin=stdin, env=env)
        rows = [json.loads(text) for text in out.stdout.splitlines() if text.strip()]
        return out.returncode, rows

    def test_version_needs_no_key(self):
        out = self.cli("channel", "version")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["supported"], [1])

    # design test 1
    def test_inbound_accept_duplicate_version_prefix_and_key(self):
        first = line(external_id="x1", sender="david", body="hi", source="desk:t1")
        code, rows = self.contract("inbound", lines=[
            first, dict(first), line(external_id="x2", sender="david", body="hi", source="tg:t1"),
            dict(first, v=2), "not an object"])
        self.assertEqual(code, 0)
        self.assertEqual([r["result"] for r in rows], ["accepted", "duplicate", "rejected", "rejected", "rejected"])
        self.assertEqual(rows[0]["event_id"], rows[1]["event_id"])
        self.assertEqual([r["error"]["code"] for r in rows[2:]], ["source_not_allowed", "bad_version", "bad_line"])
        with Store(self.home) as store:
            event = store._get("events", rows[0]["event_id"])
        self.assertEqual(event["auth"]["channel"], "desk")
        # Wrong key: nothing runs, exit 2.
        code, rows = self.contract("inbound", lines=[dict(first, external_id="x3")], key=self.other_key)
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]["error"]["code"], "auth_failed")
        # Refused inside an employee session.
        code, rows = self.contract("inbound", lines=[first], env=dict(self.env, SYNTHETICS_SESSION_RUN="run-x"))
        self.assertEqual((code, rows[0]["error"]["code"]), (2, "in_session"))
        with Store(self.home) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events WHERE data LIKE '%desk:%'").fetchone()[0], 1)

    # design test 2
    def test_question_answer_wakes_work_and_event_status(self):
        with Store(self.home) as store:
            work = store.work_put(title="needs David", key="w1")
            question = store.question_ask("david", "ok?", work_id=work["id"], key="q1")
        code, rows = self.contract("inbound", lines=[
            line(external_id="a1", sender="david", body="yes", source="desk:t", question_id=question["id"])])
        self.assertEqual(rows[0]["result"], "accepted", rows)
        event_id = rows[0]["event_id"]
        with Store(self.home) as store:
            self.assertEqual(store._get("questions", question["id"])["status"], "answered")
            self.assertEqual(store._get("works", work["id"])["status"], "ready")
        code, rows = self.contract("event-status", "--event", event_id)
        self.assertEqual((code, rows[0]["state"], rows[0]["run_id"]), (0, "accepted", None))
        with Store(self.home) as store:
            run = store.run_begin([event_id])
            # Captured but unfinished is still "accepted"; processed means the capturing run finished.
            self.assertIsNone(store.channel_event_status(self.key.read_bytes(), event_id)["run_id"])
            store.run_finish(run["id"], "done")
        code, rows = self.contract("event-status", "--event", event_id)
        self.assertEqual((rows[0]["state"], rows[0]["run_id"]), ("processed", run["id"]))
        # An event fed without this channel's stamp is not visible to it.
        with Store(self.home) as store:
            plain = store.feed("david", "plain", source="manual", external_id="p1")
        code, rows = self.contract("event-status", "--event", plain["id"])
        self.assertEqual(rows[0]["error"]["code"], "not_routed")

    # C1: an operator relaying a human's answer must say so with routed:true.
    def test_operator_answer_needs_routed(self):
        write_humans(self.home, {"default_human": "david", "senders": ["david"],
                                 "operators": {"senders": ["codex-desktop"], "source_prefixes": []}})
        with Store(self.home) as store:
            question = store.question_ask("david", "ok?", key="q2")
        answer = line(external_id="r1", sender="codex-desktop", body="David says yes", source="desk:relay",
                      question_id=question["id"])
        code, rows = self.contract("inbound", lines=[answer])
        self.assertEqual((rows[0]["result"], rows[0]["error"]["code"]), ("rejected", "invalid_state"))
        code, rows = self.contract("inbound", lines=[dict(answer, routed=True)])
        self.assertEqual(rows[0]["result"], "accepted", rows)
        with Store(self.home) as store:
            stored = store._get("questions", question["id"])
            self.assertEqual((stored["status"], stored["to_id"], stored["routed_by"]),
                             ("answered", "codex-desktop", "channel:desk"))
            actions = [r[0] for r in store.db.execute("SELECT action FROM audit ORDER BY seq")]
        self.assertIn("question.routed", actions)

    # R2: routed:true is honoured only from a sender listed in humans.json operators.senders.
    def test_routed_refused_from_non_operator(self):
        write_humans(self.home, {"default_human": "david", "senders": ["david"],
                                 "operators": {"senders": ["codex-desktop"], "source_prefixes": []}})
        with Store(self.home) as store:
            question = store.question_ask("david", "ok?", key="q3")
        forged = line(external_id="f1", sender="mallory", body="David says yes", source="desk:relay",
                      question_id=question["id"], routed=True)
        code, rows = self.contract("inbound", lines=[forged])
        self.assertEqual((rows[0]["result"], rows[0]["error"]["code"]), ("rejected", "invalid_state"), rows)
        # No humans.json at all: nobody is an operator, so even codex-desktop is refused.
        os.unlink(self.home / "humans.json")
        relayed = dict(forged, external_id="f2", sender="codex-desktop")
        code, rows = self.contract("inbound", lines=[relayed])
        self.assertEqual((rows[0]["result"], rows[0]["error"]["code"]), ("rejected", "invalid_state"), rows)
        with Store(self.home) as store:
            stored = store._get("questions", question["id"])
            self.assertEqual((stored["status"], stored.get("to_id"), stored.get("routed_by")), ("open", None, None))
            actions = [r[0] for r in store.db.execute("SELECT action FROM audit ORDER BY seq")]
            count = sum(1 for e in store.db.execute("SELECT data FROM events") if "David says yes" in e[0])
        self.assertNotIn("question.routed", actions)
        self.assertEqual(count, 0)

    def test_outbound_cycle_via_cli(self):
        with Store(self.home) as store:
            message = store.message_send("david", "report", key="o1")
            store.message_send("codex-manager", "not mine", key="o2")
        code, rows = self.contract("pending", "--instance", "i1")
        self.assertEqual([r["message_id"] for r in rows], [message["id"]])
        code, rows = self.contract("claim", "--message", message["id"], "--instance", "i1")
        self.assertEqual(rows[0]["state"], "claimed")
        ack = {"v": 1, "message_id": message["id"], "outcome": "delivered", "receipt": "desk:1"}
        code, rows = self.contract("ack", "--instance", "i1", lines=[ack, ack, dict(ack, outcome="failed")])
        self.assertEqual([r["result"] for r in rows], ["recorded", "duplicate", "rejected"])
        self.assertEqual(rows[2]["error"]["code"], "conflict")
        code, rows = self.contract("pending")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
