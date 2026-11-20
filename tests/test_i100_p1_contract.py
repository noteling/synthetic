"""#100 P1: channel contract v1 additive pieces for a multi-home adapter, through the real CLI.
- inbound optional `provider` object (provider message references), stored on the event, validated, part of the
  idempotency payload; never echoed by event-status.
- `channel identity`: the home's employee_id (Codex F11: the adapter's home pin comes from the contract, not from
  status.json).
- `channel version` lists both as `features`, so an adapter sends them only when supported (C3).
Fail-first on d99d9b1: `provider` is ignored there and `identity` is an unknown command."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from synthetics.store import Store

ROOT = Path(__file__).resolve().parents[1]


def line(**fields):
    return dict({"v": 1, "kind": "message", "thread": None, "sender": "david", "body": "hi", "source": "tg:1"},
                **fields)


class P1ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.key = base / "tg.key"
        self.key.write_text("k" * 40 + "\n")
        self.key.chmod(0o600)
        with Store(self.home) as store:
            self.me = store.initialize("Codex", "manager")
        self.env = {k: v for k, v in os.environ.items() if k not in ("SYNTHETICS_SESSION_RUN", "SYNTHETICS_HOME")}
        self.env["PYTHONPATH"] = str(ROOT)
        out = self.cli("channel", "register", "telegram", "--source-prefix", "tg:", "--key-file", str(self.key),
                       "--route", "david")
        self.assertEqual(out.returncode, 0, out.stderr)

    def cli(self, *args, stdin=None, env=None, home=None):
        return subprocess.run([sys.executable, "-m", "synthetics", "--home", str(home or self.home)] + list(args),
                              input=stdin, capture_output=True, text=True, env=env or self.env, timeout=60)

    def contract(self, command, *args, lines=None, env=None, home=None):
        stdin = "".join(json.dumps(item) + "\n" for item in lines) if lines is not None else None
        out = self.cli("channel", command, "--key-file", str(self.key), *args, stdin=stdin, env=env, home=home)
        return out.returncode, [json.loads(text) for text in out.stdout.splitlines() if text.strip()]

    def event(self, event_id):
        with Store(self.home) as store:
            return store._get("events", event_id)

    def test_version_lists_features(self):
        out = self.cli("channel", "version")
        self.assertEqual(out.returncode, 0, out.stderr)
        version = json.loads(out.stdout)
        self.assertEqual(version["supported"], [1])
        self.assertEqual(sorted(version["features"]), ["identity", "inbound.provider"])

    def test_provider_is_stored_and_idempotent(self):
        refs = {"message_id": 41, "reply_to_message_id": 40, "chat_id": 777}
        first = line(external_id="tg:1:1", provider=refs)
        code, rows = self.contract("inbound", lines=[first, dict(first),
                                                     dict(first, provider=dict(refs, message_id=42))])
        self.assertEqual(code, 0)
        self.assertEqual([r["result"] for r in rows], ["accepted", "duplicate", "rejected"])
        self.assertEqual(rows[0]["event_id"], rows[1]["event_id"])
        self.assertEqual(rows[2]["error"]["code"], "conflict")
        event = self.event(rows[0]["event_id"])
        self.assertEqual(event["provider"], refs)
        self.assertEqual(event["auth"]["channel"], "telegram")
        # Not echoed by event-status (IDs and state only).
        code, rows = self.contract("event-status", "--event", rows[0]["event_id"])
        self.assertNotIn("provider", rows[0])

    def test_without_provider_nothing_changes(self):
        plain = line(external_id="tg:1:2")
        code, rows = self.contract("inbound", lines=[plain, dict(plain)])
        self.assertEqual([r["result"] for r in rows], ["accepted", "duplicate"])
        self.assertNotIn("provider", self.event(rows[0]["event_id"]))
        # Adding provider refs to an already fed line is other content: conflict, never a silent upgrade.
        code, rows = self.contract("inbound", lines=[dict(plain, provider={"message_id": 1})])
        self.assertEqual(rows[0]["error"]["code"], "conflict")

    def test_malformed_provider_is_bad_line(self):
        bad = [
            "not an object", [1], {}, {"Message_id": 1}, {"message_id": True}, {"message_id": 1.5},
            {"message_id": {"nested": 1}}, {"message_id": ""}, {"message_id": "x" * 65},
            {"chat_id\n": 1}, {"message_id": "   "}, {"message_id": 2 ** 63}, {"message_id": -2 ** 63 - 1},
            {"k%d" % i: i for i in range(9)},
            {"a": "x" * 64, "b": "x" * 64, "c": "x" * 64, "d": "x" * 64},  # over 256 bytes
        ]
        lines = [line(external_id="tg:bad:%d" % i, provider=value) for i, value in enumerate(bad)]
        code, rows = self.contract("inbound", lines=lines)
        self.assertEqual(code, 0)
        self.assertEqual([r["result"] for r in rows], ["rejected"] * len(bad))
        self.assertEqual({r["error"]["code"] for r in rows}, {"bad_line"})
        with Store(self.home) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events WHERE data LIKE '%tg:bad%'").fetchone()[0], 0)
        # Null values and short text are fine.
        code, rows = self.contract("inbound", lines=[line(external_id="tg:ok", provider={
            "message_id": 5, "reply_to_message_id": None, "thread": "topic-1"})])
        self.assertEqual(rows[0]["result"], "accepted")
        # The signed 64-bit edges are accepted.
        code, rows = self.contract("inbound", lines=[line(external_id="tg:edge", provider={
            "chat_id": -2 ** 63, "message_id": 2 ** 63 - 1})])
        self.assertEqual(rows[0]["result"], "accepted")

    def test_identity(self):
        code, rows = self.contract("identity")
        self.assertEqual(code, 0)
        self.assertEqual(rows, [{"v": 1, "employee_id": self.me["id"], "name": "Codex", "error": None}])

    def test_identity_is_keyed_and_refused_in_session(self):
        other = Path(self.tmp.name) / "other.key"
        other.write_text("o" * 40 + "\n")
        other.chmod(0o600)
        out = self.cli("channel", "identity", "--key-file", str(other))
        self.assertEqual(out.returncode, 2)
        self.assertEqual(json.loads(out.stdout)["error"]["code"], "auth_failed")
        self.assertNotIn(self.me["id"], out.stdout)
        code, rows = self.contract("identity", env=dict(self.env, SYNTHETICS_SESSION_RUN="run-x"))
        self.assertEqual((code, rows[0]["error"]["code"]), (2, "in_session"))
        self.assertNotIn("employee_id", rows[0])


if __name__ == "__main__":
    unittest.main()
