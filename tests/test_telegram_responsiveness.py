"""Functional regressions for delivery while the inbound channel is quiet/backing off."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from synthetics.store import Store
from synthetics.telegram import TelegramAdapter, TelegramAPIError, TelegramChannelState


class TelegramResponsivenessTests(unittest.TestCase):
    def assert_outgoing_is_not_blocked(self, rate_limited=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "employee"
            with Store(home) as store:
                identity = store.initialize("Aster", "Colleague")
            state_path = home / "telegram.json"
            state = TelegramChannelState(state_path)
            state.employee_id = identity["id"]
            state.bot_id = 123
            state.bot_username = "test_bot"
            state.paired = True
            state.bound_user_id = 42
            state.bound_chat_id = 42
            state.save()
            config = root / "config.json"
            config.write_text(json.dumps(dict(token_file=str(root / "unused-token"),
                                              employee_home=str(home), state_file=str(state_path),
                                              colleague_id="david", local_thread="conversation")))
            polling, sent, stop = threading.Event(), threading.Event(), threading.Event()
            errors, requests = [], []

            class Provider:
                def get_me(self):
                    return dict(id=123, username="test_bot", is_bot=True)

                def get_webhook_info(self):
                    return dict(url="")

                def get_updates(self, offset=None, timeout=30):
                    if stop.is_set():
                        raise KeyboardInterrupt()
                    requests.append(timeout)
                    polling.set()
                    if rate_limited:
                        raise TelegramAPIError("Inbound rate limited", error_code=429, retry_after=10)
                    # Model the provider's actual long-poll contract. The test
                    # does not release the poll merely because outbox work exists.
                    if stop.wait(timeout):
                        raise KeyboardInterrupt()
                    return []

                def send_message(self, chat_id, text, **kwargs):
                    if chat_id != 42 or text != "An unsolicited update":
                        raise AssertionError("Unexpected outgoing content or recipient")
                    sent.set()
                    return dict(message_id=100)

            def sleep(seconds):
                if stop.wait(seconds):
                    raise KeyboardInterrupt()

            adapter = TelegramAdapter(str(config))
            provider = Provider()

            def run():
                try:
                    adapter.run()
                except BaseException as error:
                    if not isinstance(error, KeyboardInterrupt):
                        errors.append(error)

            with patch.object(adapter, "_make_client", return_value=provider), \
                    patch("synthetics.telegram.time.sleep", side_effect=sleep):
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                try:
                    self.assertTrue(polling.wait(3), "Adapter did not begin polling")
                    with Store(home) as store:
                        store.message_send("david", "An unsolicited update", key="fresh-update", thread="conversation")
                    self.assertTrue(sent.wait(1.5),
                                    "A fresh outgoing message was blocked by the incoming poll/backoff")
                finally:
                    stop.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive(), "Test adapter failed to stop")
                self.assertEqual(errors, [])
                if rate_limited:
                    self.assertLessEqual(len(requests), 2, "Inbound backoff must still prevent poll flooding")

    def test_new_outgoing_message_is_delivered_while_incoming_poll_is_quiet(self):
        self.assert_outgoing_is_not_blocked()

    def test_incoming_rate_limit_does_not_block_outgoing_delivery(self):
        self.assert_outgoing_is_not_blocked(rate_limited=True)


if __name__ == "__main__":
    unittest.main()
