"""M22 (N1 + N2): evidence needs authenticated inputs and recorded deliveries."""
import contextlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics.brain import ClaudeBrain
from synthetics.cli import main
from synthetics.store import SESSION_ENV, Store

PACKAGE = str(Path(__file__).resolve().parent.parent)


def no_session_env():
    """CLI tests must not depend on the ambient env: a worker session sets SYNTHETICS_SESSION_RUN."""
    env = dict(os.environ)
    env.pop(SESSION_ENV, None)
    return mock.patch.dict(os.environ, env, clear=True)


class AuthChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "employee"
        self.store = Store(self.home)
        self.me = self.store.initialize("Rowan", "Noteling maintainer")["id"]
        self.key = b"f" * 64
        self.store.channel_register("fieldpost", Store.credential_hash(self.key), ["fieldpost"], "test",
                                    routes=["employee-colleague"])
        self.desk = b"d" * 64
        self.store.channel_register("codex-desktop", Store.credential_hash(self.desk), ["codex-desktop:"], "test")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def cli(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--home", str(self.home)] + list(args))
        return code, out.getvalue(), err.getvalue()

    def keyfile(self, name, data):
        path = self.root / name
        path.write_bytes(data + b"\n")
        path.chmod(0o600)
        return path

    def done(self, work_id, *evidence):
        return self.store.work_put(id=work_id, status="done", evidence=[dict(level=l, ref=r) for l, r in evidence])

    # --- N1: spoofing ---------------------------------------------------------
    def test_shell_spoof_of_colleague_caps_at_observed(self):
        with no_session_env():
            code, out, _ = self.cli("feed", "LGTM", "--from", "employee-colleague", "--source", "fieldpost")
        self.assertEqual(code, 0)
        spoof = json.loads(out)
        self.assertIsNone(spoof["auth"])
        work = self.store.work_put("Ship")
        with self.assertRaisesRegex(ValueError, "at most observed"):
            self.done(work["id"], ("verified", spoof["id"]))
        closed = self.done(work["id"], ("observed", spoof["id"]))
        self.assertIsNone(closed["evidence"][0]["check"]["auth"])

    def test_spoofed_answer_wakes_work_but_cannot_verify(self):
        work = self.store.work_put("Ship")
        question = self.store.question_ask("david", "OK to ship?", work["id"], key="q")
        self.store.feed("david", "yes", reply_to=question["id"])  # shell-shaped, no credential
        self.assertEqual(self.store.work_get(work["id"])["status"], "ready")
        with self.assertRaisesRegex(ValueError, "question evidence .* at most observed"):
            self.done(work["id"], ("verified", question["id"]))
        self.done(work["id"], ("observed", question["id"]))

    def test_authenticated_relay_input_verifies(self):
        event = self.store.feed("employee-colleague", "LGTM", source="fieldpost", credential=self.key)
        self.assertEqual(event["auth"]["channel"], "fieldpost")
        work = self.store.work_put("Ship")
        closed = self.done(work["id"], ("verified", event["id"]))
        self.assertEqual(closed["evidence"][0]["check"]["auth"], "fieldpost")

    def test_wrong_key_and_foreign_prefix_raise(self):
        with self.assertRaisesRegex(ValueError, "does not match a registered channel"):
            self.store.feed("david", "x", source="fieldpost", credential=b"z" * 64)
        with self.assertRaisesRegex(ValueError, "may not write source"):
            self.store.feed("david", "x", source="telegram:1", credential=self.key)
        # Operator notices with a free-form source must not be given the desktop key.
        with self.assertRaisesRegex(ValueError, "may not write source"):
            self.store.feed("codex-desktop", "notice", source="operator-runtime-cutover", credential=self.desk)
        self.assertEqual([e for e in self.store.event_pending()], [])

    def test_desktop_operator_command_answers_verified(self):
        work = self.store.work_put("Decide D1")
        question = self.store.question_ask("david", "Which store?", work["id"], key="d1")
        keyfile = self.keyfile("codex-desktop.key", self.desk)
        with no_session_env():
            code, out, _ = self.cli("feed", "Option B", "--from", "david", "--source", "codex-desktop:thread-9",
                                    "--thread", "codex:abc", "--external-id", "item-1", "--reply-to", question["id"],
                                    "--credential-file", str(keyfile))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["auth"]["channel"], "codex-desktop")
        closed = self.done(work["id"], ("verified", question["id"]))
        self.assertEqual(closed["evidence"][0]["check"]["auth"], "codex-desktop")

    def test_redelivery_returns_first_event_unchanged(self):
        first = self.store.feed("david", "x", source="codex-desktop:t", external_id="i1")
        again = self.store.feed("david", "x", source="codex-desktop:t", external_id="i1", credential=self.desk)
        self.assertEqual(first["id"], again["id"])
        self.assertIsNone(self.store.event_get(first["id"])["auth"])

    def test_credential_file_must_be_private(self):
        keyfile = self.keyfile("loose.key", self.desk)
        keyfile.chmod(0o644)
        with no_session_env():
            code, _, err = self.cli("feed", "x", "--from", "david", "--source", "codex-desktop:t",
                                    "--credential-file", str(keyfile))
        self.assertNotEqual(code, 0)
        self.assertIn("chmod 600", err)

    # --- session marker ---------------------------------------------------------
    def test_session_marker_refuses_credential_and_channel_operations(self):
        keyfile = self.keyfile("codex-desktop.key", self.desk)
        with mock.patch.dict(os.environ, {SESSION_ENV: "run-x"}):
            for args in (("feed", "x", "--from", "david", "--source", "codex-desktop:t",
                          "--credential-file", str(keyfile)),
                         ("ingest", "--credential-file", str(keyfile)),
                         ("channel", "register", "x", "--source-prefix", "x:", "--key-file", str(keyfile)),
                         ("channel", "provision", "x", "--source-prefix", "x:", "--home", str(self.home),
                          "--key-dir", str(self.root / "keys"))):
                code, _, err = self.cli(*args)
                self.assertNotEqual(code, 0, args)
                self.assertIn("refused inside an employee session", err)
        self.assertFalse((self.root / "keys").exists())
        # Without a credential, feed still works inside a session (capped evidence).
        with mock.patch.dict(os.environ, {SESSION_ENV: "run-x"}):
            code, out, _ = self.cli("feed", "note", "--from", "david")
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)["auth"])

    def test_model_store_cannot_register_or_backfill(self):
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as worker:
            with self.assertRaisesRegex(ValueError, "operator operation"):
                worker.channel_register("x", "a" * 64, ["x:"], "model")
            event = self.store.feed("employee-colleague", "x", source="fieldpost")
            with self.assertRaisesRegex(ValueError, "operator operation"):
                worker.event_auth_backfill(event["id"], self.key, "journal")

    def test_provision_writes_private_key_registers_hash_never_prints_key(self):
        other = self.root / "other"
        Store(other).initialize("Other", "Peer").get("id")
        keys = self.root / "keys"
        with no_session_env():
            code, out, _ = self.cli("channel", "provision", "relay", "--source-prefix", "fieldpost",
                                    "--home", str(self.home), "--home", str(other), "--key-dir", str(keys))
        self.assertEqual(code, 0)
        result = json.loads(out)
        keyfile = keys / "relay.key"
        key = keyfile.read_bytes().strip()
        self.assertEqual(stat.S_IMODE(keyfile.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(keys.stat().st_mode), 0o700)
        self.assertNotIn(key.decode(), out)
        self.assertEqual(len(result["homes"]), 2)
        with Store(other) as peer:
            self.assertEqual(peer.channel_list()[0]["key_sha256"], Store.credential_hash(key))
        audit = json.dumps(self.store.audit_list(200))
        self.assertNotIn(key.decode(), audit)
        # Idempotent: rerun keeps the key and registration.
        with no_session_env():
            code, out2, _ = self.cli("channel", "provision", "relay", "--source-prefix", "fieldpost",
                                     "--home", str(self.home), "--key-dir", str(keys))
        self.assertFalse(json.loads(out2)["key_created"])
        self.assertEqual(keyfile.read_bytes().strip(), key)

    # --- N2: deliveries and receipts --------------------------------------------
    def test_delivery_record_makes_message_observed(self):
        message = self.store.message_send("employee-colleague", "Shipped", key="m")
        work = self.store.work_put("Ship")
        with self.assertRaisesRegex(ValueError, "at most attempted"):
            self.done(work["id"], ("observed", message["id"]))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.store.delivery_record(message["id"], b"z" * 64, receipt="fieldpost:event-x")
        self.store.delivery_record(message["id"], self.key, receipt="fieldpost:event-x")
        self.store.delivery_record(message["id"], self.key, receipt="fieldpost:event-x")  # idempotent
        self.assertEqual(len(self.store.outbox_list()[0]["deliveries"]), 1)
        with self.assertRaisesRegex(ValueError, "at most observed"):
            self.done(work["id"], ("verified", message["id"]))
        closed = self.done(work["id"], ("observed", message["id"]))
        self.assertEqual(closed["evidence"][0]["check"]["delivered_by"], "fieldpost")

    def test_free_form_receipts_cannot_complete_work(self):
        work = self.store.work_put("Ship")
        for ref in ("receipt:fieldpost:whatever", "https://example.com/proof"):
            with self.assertRaisesRegex(ValueError, "at most attempted"):
                self.done(work["id"], ("observed", ref))
            with self.assertRaisesRegex(ValueError, "attempted alone"):
                self.done(work["id"], ("attempted", ref))

    # --- model visibility ----------------------------------------------------------
    def test_snapshot_event_get_and_thread_history_show_auth(self):
        legacy = self.store.feed("david", "old", thread="t")
        # A pre-M22 event row has no auth key at all.
        row = json.loads(self.store.db.execute("SELECT data FROM events WHERE id=?", (legacy["id"],)).fetchone()[0])
        row.pop("auth")
        self.store.db.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(row), legacy["id"]))
        authed = self.store.feed("david", "new", thread="t", source="codex-desktop:t", credential=self.desk)
        context = self.store.context()
        by_id = {e["id"]: e for e in context["events"]}
        self.assertIsNone(by_id[legacy["id"]]["auth"])
        self.assertEqual(by_id[authed["id"]]["auth"]["channel"], "codex-desktop")
        items = {i["id"]: i for i in context["threads"]["t"]["items"]}
        self.assertIn("auth", items[legacy["id"]])
        self.assertIsNone(items[legacy["id"]]["auth"])
        self.assertEqual(items[authed["id"]]["auth"]["channel"], "codex-desktop")
        self.assertIsNone(self.store.event_get(legacy["id"])["auth"])
        prompt = (Path(PACKAGE) / "synthetics" / "prompts" / "employee.md").read_text()
        self.assertIn("`auth`", prompt)

    # --- migration and backfill ----------------------------------------------------
    def test_v2_home_migrates_to_v3_and_legacy_events_cap_at_observed(self):
        legacy = self.store.feed("employee-colleague", "approved", source="fieldpost")
        self.store.close()
        db = sqlite3.connect(self.home / "state.sqlite3")
        db.execute("DROP TABLE channels")
        db.execute("PRAGMA user_version=2")
        db.commit()
        db.close()
        self.store = Store(self.home)
        # Migrates through v3 to the current version (M21: 4).
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], Store.SCHEMA_VERSION)
        self.assertEqual(self.store.channel_list(), [])
        work = self.store.work_put("Ship")
        with self.assertRaisesRegex(ValueError, "at most observed"):
            self.done(work["id"], ("verified", legacy["id"]))

    def test_backfill_is_corroborated_idempotent_and_audited(self):
        legacy = self.store.feed("employee-colleague", "approved", source="fieldpost")
        stamped = self.store.event_auth_backfill(legacy["id"], self.key, "journal:message-1")
        self.assertTrue(stamped["auth"]["backfill"])
        again = self.store.event_auth_backfill(legacy["id"], self.key, "journal:message-1")
        self.assertEqual(again["auth"], stamped["auth"])
        actions = [a["action"] for a in self.store.audit_list(50)]
        self.assertEqual(actions.count("event.auth_backfilled"), 1)
        desk = self.store.feed("david", "x", source="codex-desktop:t")
        with self.assertRaisesRegex(ValueError, "may not write source"):
            self.store.event_auth_backfill(desk["id"], self.key, "journal:nope")
        work = self.store.work_put("Ship")
        self.done(work["id"], ("verified", legacy["id"]))


class BrainSessionEnvTests(unittest.TestCase):
    def test_session_marker_set_and_auto_memory_stays_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            script = Path(tmp) / "claude"
            script.write_text(
                "#!%s\n"
                "import json, os, sys\n"
                "sys.stdin.read()\n"
                "assert os.environ['SYNTHETICS_SESSION_RUN'] == 'run-abc', os.environ.get('SYNTHETICS_SESSION_RUN')\n"
                "assert 'CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD' not in os.environ\n"
                "assert '--settings' not in sys.argv[1:]\n"
                "print(json.dumps({'type': 'result', 'result': 'ok', 'total_cost_usd': 0, 'is_error': False}))\n"
                % sys.executable)
            script.chmod(0o700)
            env = dict(os.environ)
            env.pop(SESSION_ENV, None)
            env.pop("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", None)
            with mock.patch.dict(os.environ, env, clear=True):
                result = ClaudeBrain(home, executable=str(script)).run({"run": {"id": "run-abc"}})
            self.assertEqual(result.summary, "ok")

    def test_cli_subprocess_refuses_in_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            Store(home).initialize("A", "B")
            env = dict(os.environ, PYTHONPATH=PACKAGE, **{SESSION_ENV: "run-1"})
            proc = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(home), "channel", "provision",
                                   "x", "--source-prefix", "x:", "--home", str(home), "--key-dir", str(Path(tmp) / "k")],
                                  env=env, capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("refused inside an employee session", proc.stderr)
            env.pop(SESSION_ENV)
            proc = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(home), "channel", "provision",
                                   "x", "--source-prefix", "x:", "--home", str(home), "--key-dir", str(Path(tmp) / "k")],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
