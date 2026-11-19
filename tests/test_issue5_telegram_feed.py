"""Issue #5: Telegram feed errors are classified, retried with backoff, and checked at startup."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics.cli import main
from synthetics.store import Store
import synthetics.telegram as _tg
from synthetics.telegram import (TelegramAdapter, TelegramChannelState, UpdateProcessor,
                                 TRANSIENT_BACKOFF_INITIAL)

# Resolved per test so the tests fail one by one (not at import) on code without the fix.
ChannelConfigError = getattr(_tg, "ChannelConfigError", type("ChannelConfigErrorMissing", (Exception,), {}))
from tests.humans_fixture import write_host_humans

KEY = b"k" * 32
OTHER_KEY = b"z" * 32
BOT = 55


def _update(update_id, text="hello", user_id=42):
    return {"update_id": update_id, "message": {"message_id": update_id, "date": 1700000000, "text": text,
                                                "chat": {"id": 42, "type": "private"},
                                                "from": {"id": user_id, "is_bot": False}}}


class _Provider:
    """Fake Bot API: serves batches, then stops the loop."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.offsets = []

    def get_me(self):
        return {"id": BOT, "username": "testbot", "is_bot": True}

    def get_webhook_info(self):
        return {"url": ""}

    def get_updates(self, offset=None, timeout=None):
        self.offsets.append(offset)
        if not self.batches:
            raise KeyboardInterrupt()
        return self.batches.pop(0)


class Issue5Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "emp"
        with Store(self.home) as store:
            self.identity = store.initialize("Aster", "issue 5 colleague")
            store.channel_register("telegram", Store.credential_hash(KEY), ["telegram:"], "operator", routes=["david"])
        self.state_path = self.home / "telegram_state.json"
        state = TelegramChannelState(self.state_path)
        state.employee_id = self.identity["id"]
        state.bot_id, state.bot_username = BOT, "testbot"
        state.paired, state.bound_user_id, state.bound_chat_id = True, 42, 42
        state.save()

    def adapter(self, key=KEY):
        token = self.root / "token.txt"
        token.write_text("test-token")
        keyfile = self.root / "channel.key"
        keyfile.write_bytes(key)
        keyfile.chmod(0o600)
        cfg = self.root / "cfg.json"
        cfg.write_text(json.dumps({"token_file": str(token), "colleague_id": "david", "employee_home": str(self.home),
                                   "state_file": str(self.state_path), "lock_file": str(self.root / "tg.lock"),
                                   "credential_file": str(keyfile)}))
        return TelegramAdapter(str(cfg))

    def run_loop(self, adapter, provider):
        sleeps = []
        with mock.patch.object(adapter, "_make_client", return_value=provider), \
                mock.patch.object(adapter, "_sleep_servicing_outbox",
                                  side_effect=lambda seconds, *a, **k: sleeps.append(seconds)), \
                mock.patch.object(adapter, "_service_activity"), \
                mock.patch("synthetics.telegram.OutboxDelivery.process_outbox"):
            adapter.run()
        return sleeps

    def test_per_update_refusal_is_recorded_and_cursor_advances(self):
        """A deterministic per-update ValueError no longer blocks later updates."""
        state = TelegramChannelState(self.state_path)
        proc = UpdateProcessor(state, "david", credential=KEY)
        with Store(self.home) as store:
            with mock.patch.object(store, "feed", side_effect=ValueError("bad body for this update")):
                result = proc.process(_update(7, "secret words"), store)
        self.assertEqual(result["reason"], "rejected")
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["feed_rejections_total"], 1)
        self.assertEqual(saved["feed_rejections"][-1]["update_id"], 7)
        self.assertIn("bad body", saved["feed_rejections"][-1]["error"])
        self.assertNotIn("secret words", self.state_path.read_text())

        provider = _Provider([[_update(8)], [_update(9)]])
        adapter = self.adapter()
        with mock.patch.object(UpdateProcessor, "process",
                               side_effect=[{"processed": False, "reason": "rejected", "event_id": None},
                                            {"processed": True, "reason": "text", "event_id": "event-x"}]):
            self.run_loop(adapter, provider)
        self.assertEqual(provider.offsets, [None, 9, 10])
        self.assertEqual(json.loads(self.state_path.read_text())["update_cursor"], 9)

    def test_store_error_is_retried_with_backoff_without_advancing(self):
        provider = _Provider([[_update(1)], [_update(1)], [_update(1)], [_update(1)]])
        adapter = self.adapter()
        err = {"processed": False, "reason": "store_error", "event_id": None}
        ok = {"processed": True, "reason": "text", "event_id": "event-x"}
        with mock.patch.object(UpdateProcessor, "process", side_effect=[err, err, ok, err]):
            sleeps = self.run_loop(adapter, provider)
        # doubling while it keeps failing, reset after a clean batch
        self.assertEqual(sleeps, [TRANSIENT_BACKOFF_INITIAL, TRANSIENT_BACKOFF_INITIAL * 2, TRANSIENT_BACKOFF_INITIAL])
        self.assertEqual(provider.offsets, [None, None, None, 2, 2])

    def test_unregistered_key_refused_at_startup_before_polling(self):
        provider = _Provider([[_update(1)]])
        with self.assertRaises(ChannelConfigError) as caught:
            self.run_loop(self.adapter(key=OTHER_KEY), provider)
        self.assertIn("does not match a registered channel", str(caught.exception))
        self.assertEqual(provider.offsets, [])

    def test_source_prefix_not_allowed_refused_at_startup(self):
        with Store(self.home) as store:
            store.channel_register("telegram", Store.credential_hash(KEY), ["mail:"], "operator", routes=["david"])
        provider = _Provider([[_update(1)]])
        with self.assertRaises(ChannelConfigError) as caught:
            self.run_loop(self.adapter(), provider)
        self.assertIn("may not write source telegram:55", str(caught.exception))
        self.assertEqual(provider.offsets, [])

    def test_channel_broken_mid_run_stops_without_advancing_or_dropping(self):
        """Key unregistered after startup: every update would fail, so stop and keep the cursor."""
        state = TelegramChannelState(self.state_path)
        state.update_cursor = 3
        state.save()
        proc = UpdateProcessor(TelegramChannelState(self.state_path), "david", credential=OTHER_KEY)
        with Store(self.home) as store:
            before = store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            with self.assertRaises(ChannelConfigError):
                proc.process(_update(4), store)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["update_cursor"], 3)
        self.assertEqual(saved.get("feed_rejections", []), [])

    def test_status_shows_feed_rejections(self):
        write_host_humans(self.home)
        token = self.root / "tok"
        token.write_text("test-only-token")
        token.chmod(0o600)
        def command(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["--home", str(self.home), "telegram"] + list(args))
            return rc, out.getvalue(), err.getvalue()
        rc, out, err = command("init", "--token-file", str(token), "--thread", "human-one")
        self.assertEqual(rc, 0, err)
        state_file = Path(json.loads(Path(json.loads(out)["config"]).read_text())["state_file"])
        state = TelegramChannelState(state_file)
        state.record_feed_rejection(11, "refused")
        rc, out, err = command("status")
        self.assertEqual(rc, 0, err)
        value = json.loads(out)
        self.assertEqual(value["feed_rejections"], 1)
        self.assertEqual(value["last_feed_rejection"]["update_id"], 11)

    def test_serve_reports_channel_config_error_text(self):
        """The serve wrapper hides provider errors (token URLs) but shows our own config message."""
        write_host_humans(self.home)
        token = self.root / "tok"
        token.write_text("test-only-token")
        token.chmod(0o600)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(["--home", str(self.home), "telegram", "init", "--token-file", str(token),
                                   "--thread", "human-one"]), 0)
        def broken_run(self_):
            raise ChannelConfigError("Telegram channel misconfigured: the credential does not match a registered channel")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(TelegramAdapter, "run", broken_run), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["--home", str(self.home), "telegram", "serve"])
        self.assertEqual(rc, 1)
        self.assertIn("does not match a registered channel", err.getvalue())


if __name__ == "__main__":
    unittest.main()
