"""Satchel P2 r1 (#111, Rowan round 1 + Codex ruling): atomic recreate, hold by channel state, sealed home refuses
serve/feed/channel, configured Telegram lock, capture hold/release, runtime.json claude path. Synthetic fixtures."""

import io
import json
import os
from pathlib import Path
import secrets
import sys
from contextlib import redirect_stderr, redirect_stdout
import unittest
from unittest import mock

from synthetics.store import ChannelError, Store
from tests.test_satchel_p2 import Fixture, satchel


def partials(root):
    return sorted(p.name for p in Path(root).iterdir() if ".partial-" in p.name)


class AtomicRecreateF1(Fixture):
    def test_identity_mismatch_leaves_new_absent_and_retry_works(self):
        archive = self.extract()["archive"]
        real = Store.identity
        with mock.patch.object(Store, "identity", lambda store: dict(real(store), id="employee-someone-else")):
            self.assertRefused("identity_mismatch", self.recreate, archive, source_lost=True, attest="lost")
        self.assertFalse((self.root / "new").exists())
        self.assertEqual(partials(self.root), [])
        result = self.recreate(archive, source_lost=True, attest="lost")
        self.assertEqual(sorted(result["held_messages"]), sorted([self.pending["id"], self.claimed["id"]]))
        self.assertTrue((self.root / "new" / "REPROVISION.md").is_file())

    def test_hold_failure_leaves_new_absent(self):
        archive = self.extract()["archive"]
        with mock.patch.object(Store, "_hold_unfinished", side_effect=RuntimeError("injected hold failure")):
            with self.assertRaises(RuntimeError):
                self.recreate(archive, source_lost=True, attest="lost")
        self.assertFalse((self.root / "new").exists())
        self.assertEqual(partials(self.root), [])
        self.recreate(archive, source_lost=True, attest="lost")
        self.assertTrue((self.root / "new" / "REPROVISION.md").is_file())

    def test_hold_runs_in_restore_transaction(self):
        """Rowan P1b: a failed hold must not leave grants revoked and runs interrupted with nothing held."""
        store = Store(self.home)
        self.addCleanup(store.close)
        store.run_begin()
        with mock.patch.object(Store, "_hold_unfinished", side_effect=RuntimeError("injected hold failure")):
            with self.assertRaises(RuntimeError):
                satchel._restore_store(store, "backup", "t", "lost", "a.tar", {})
        self.assertIsNone(store._get("access_grants", self.grant["id"])["revoked_at"])
        self.assertEqual([r["status"] for r in store._all("runs")], ["running"])
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM audit WHERE action='satchel.restored'").fetchone()[0], 0)

    def test_into_existing_empty_dir_and_reprovision_names_final_path(self):
        archive = self.extract()["archive"]
        new = self.root / "new"
        new.mkdir()
        self.recreate(archive, source_lost=True, attest="lost")
        text = (new / "REPROVISION.md").read_text()
        self.assertIn("--home %s home held" % new, text)
        self.assertNotIn(".partial-", text)
        self.assertEqual(partials(self.root), [])
        self.assertEqual(oct(new.stat().st_mode & 0o777), oct(0o700))


class HoldByChannelState(Fixture):
    def two_channels(self, store):
        key_b = secrets.token_hex(32)
        store.channel_register("desk-b", Store.credential_hash(key_b), ["deskb:"], "operator", routes=["colleague"])
        return key_b

    def test_delivered_on_a_pending_on_b_is_held(self):
        store = Store(self.home)
        self.addCleanup(store.close)
        key_b = self.two_channels(store)
        both = store.message_send("colleague", "two channels", key="m-both")
        store.channel_claim(key_b, both["id"])
        store.channel_ack(key_b, both["id"], "failed", retryable=True)
        store.channel_claim(self.key, both["id"])
        store.channel_ack(self.key, both["id"], "delivered", receipt="rA")
        states = {k: v["state"] for k, v in store._get("messages", both["id"])["channel_states"].items()}
        self.assertEqual(sorted(states.values()), ["delivered", "pending"])
        self.assertEqual(store.delivery_summary(store._get("messages", both["id"])), "delivered")
        held = store.outbox_hold_unfinished("restored")
        self.assertIn(both["id"], held)
        self.assertNotIn(self.delivered["id"], held)

    def test_uncertain_message_is_held(self):
        store = Store(self.home)
        self.addCleanup(store.close)
        unsure = store.message_send("colleague", "unsure", key="m-unsure")
        store.channel_claim(self.key, unsure["id"])
        store.channel_ack(self.key, unsure["id"], "uncertain")
        self.assertEqual(store._get("messages", unsure["id"])["channel_states"]["desk"]["state"], "uncertain")
        self.assertIn(unsure["id"], store.outbox_hold_unfinished("restored"))

    def test_all_failed_and_delivered_not_held(self):
        store = Store(self.home)
        self.addCleanup(store.close)
        failed = store.message_send("colleague", "failed", key="m-failed")
        store.channel_claim(self.key, failed["id"])
        store.channel_ack(self.key, failed["id"], "failed")
        held = store.outbox_hold_unfinished("restored")
        self.assertNotIn(failed["id"], held)
        self.assertNotIn(self.delivered["id"], held)
        self.assertEqual(sorted(held), sorted([self.pending["id"], self.claimed["id"]]))


class SealedHome(Fixture):
    def seal(self):
        satchel.extract_move(self.home, self.out, temp_base=self.tmpbase)
        self.assertTrue((self.home / "SEALED.json").is_file())

    def test_serve_on_sealed_home_exits_zero(self):
        self.seal()
        from synthetics import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--home", str(self.home), "serve", "--brain", "codex", "--codex", sys.executable,
                           "--allow-claude", sys.executable])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("sealed", err.getvalue())

    def test_feed_and_channel_refuse_sealed(self):
        self.seal()
        store = Store(self.home)
        self.addCleanup(store.close)
        before = store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        calls = (lambda: store.feed("colleague", "hello after move", source="manual"),
                 lambda: store.channel_pending(self.key),
                 lambda: store.channel_claim(self.key, self.pending["id"]),
                 lambda: store.channel_ack(self.key, self.claimed["id"], "delivered", receipt="late"))
        for call in calls:
            with self.assertRaises(ChannelError) as caught:
                call()
            self.assertEqual(caught.exception.code, "sealed")
            self.assertIn("sealed", str(caught.exception))
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)
        self.assertEqual(store._get("messages", self.claimed["id"])["channel_states"]["desk"]["state"], "claimed")

    def test_channel_command_on_sealed_home_is_whole_command_failure(self):
        self.seal()
        from synthetics import cli
        keyfile = self.root / "desk.key"
        keyfile.write_text(self.key)
        keyfile.chmod(0o600)
        inbound = json.dumps({"v": 1, "external_id": "x1", "sender": "colleague", "body": "after move",
                              "source": "desk:x"}) + "\n"
        for command, stdin in (("pending", ""), ("inbound", inbound)):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
                rc = cli.main(["--home", str(self.home), "channel", command, "--key-file", str(keyfile)])
            self.assertEqual(rc, 2, (command, out.getvalue(), err.getvalue()))
            line = json.loads(out.getvalue().splitlines()[-1])
            self.assertEqual(line["error"]["code"], "sealed", command)


class TelegramLockL3(Fixture):
    def hold(self, path):
        import fcntl
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(path), "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(handle.close)

    def test_configured_absolute_lock_file(self):
        lock = self.root / "elsewhere" / "tg.lock"
        (self.home / "telegram.json").write_text(json.dumps({"token_file": "/x", "lock_file": str(lock)}))
        self.hold(lock)
        self.assertRefused("live", satchel.extract_move, self.home, self.out, temp_base=self.tmpbase)
        self.assertFalse((self.home / "SEALED.json").exists())

    def test_configured_relative_lock_file(self):
        (self.home / "telegram.json").write_text(json.dumps({"version": 2, "lock_file": "run/tg.lock"}))
        self.hold(self.home / "run" / "tg.lock")
        self.assertRefused("live", satchel.extract_move, self.home, self.out, temp_base=self.tmpbase)

    def test_default_lock_still_checked(self):
        self.hold(self.home / "telegram.lock")
        self.assertRefused("live", satchel.extract_move, self.home, self.out, temp_base=self.tmpbase)

    def test_unreadable_config_refuses(self):
        (self.home / "telegram.json").write_text("{not json")
        self.assertRefused("telegram_config", satchel.extract_move, self.home, self.out, temp_base=self.tmpbase)
        self.assertFalse((self.home / "SEALED.json").exists())


class CapturesHeld(Fixture):
    def add_capture(self):
        conn = Store(self.home)
        try:
            conn.db.execute("INSERT INTO capture_outbox(event_id,project,mode,binding_id,direction,status,attempts,"
                            "created_at) VALUES ('event-cap1','proj','capture',NULL,'in','pending',0,'2026-11-21T00:00:00Z')")
        finally:
            conn.close()

    def statuses(self, home):
        store = Store(home)
        try:
            return [r[0] for r in store.db.execute("SELECT status FROM capture_outbox")]
        finally:
            store.close()

    def test_backup_holds_captures_until_release(self):
        self.add_capture()
        result = self.recreate(self.extract()["archive"], source_lost=True, attest="lost")
        self.assertEqual(result["held_captures"], 1)
        new = self.root / "new"
        self.assertEqual(self.statuses(new), ["held"])
        self.assertIn("1 project captures are held", (new / "REPROVISION.md").read_text())
        store = Store(new)
        try:
            self.assertEqual(store.capture_release_held(by="david"), {"released": 1})
        finally:
            store.close()
        self.assertEqual(self.statuses(new), ["pending"])

    def test_move_does_not_hold_captures(self):
        self.add_capture()
        archive = satchel.extract_move(self.home, self.out, temp_base=self.tmpbase)["archive"]
        result = self.recreate(archive, from_move=True)
        self.assertEqual(result["held_captures"], 0)
        self.assertEqual(self.statuses(self.root / "new"), ["pending"])


class RuntimeJsonClaude(Fixture):
    def test_non_executable_claude_path_removed_and_noted(self):
        (self.home / "runtime.json").write_text(json.dumps({"claude": "/nonexistent/claude", "budget": 7}))
        self.recreate(self.extract()["archive"], source_lost=True, attest="lost")
        new = self.root / "new"
        settings = json.loads((new / "runtime.json").read_text())
        self.assertNotIn("claude", settings)
        self.assertEqual(settings["budget"], 7)
        self.assertIn("`claude` path /nonexistent/claude is not executable here; removed",
                      (new / "REPROVISION.md").read_text())

    def test_executable_claude_path_kept(self):
        (self.home / "runtime.json").write_text(json.dumps({"claude": sys.executable}))
        self.recreate(self.extract()["archive"], source_lost=True, attest="lost")
        self.assertEqual(json.loads((self.root / "new" / "runtime.json").read_text())["claude"], sys.executable)


if __name__ == "__main__":
    unittest.main()
