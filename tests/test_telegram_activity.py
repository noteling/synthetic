"""Visible activity while a real employee run handles a human message."""

import json
import os
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from synthetics.brain import BrainResult
from synthetics.engine import Engine, WorkerLock
from synthetics.store import Store
from synthetics.telegram import (CHAT_ACTION_INTERVAL, CHAT_ACTION_TIMEOUT, OutboxDelivery,
                                 TelegramAdapter, TelegramAPIError, TelegramChannelState,
                                 TelegramClient, TelegramNetworkError)


class TelegramActivityOperationTests(unittest.TestCase):
    def test_typing_is_visible_before_a_slow_employee_reply_without_extra_messages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "employee"
            with Store(home) as store:
                identity = store.initialize("Aster", "Colleague")
                onboarding = store.run_begin()
                store.run_finish(onboarding["id"], "Fixture onboarding complete")
            state_path = home / "channel.state.json"
            state = TelegramChannelState(state_path)
            state.employee_id = identity["id"]
            state.bot_id = 123
            state.bot_username = "test_bot"
            state.paired = True
            state.bound_user_id = 42
            state.bound_chat_id = 42
            state.save()
            config = root / "channel.json"
            config.write_text(json.dumps(dict(token_file=str(root / "unused-token"),
                                              employee_home=str(home), state_file=str(state_path),
                                              colleague_id="david", local_thread="conversation")))
            listening, started, release, typing, replied, stop = (threading.Event() for _ in range(6))
            sent, actions, errors, updates = [], [], [], []

            class Provider:
                def get_me(self):
                    return dict(id=123, username="test_bot", is_bot=True)

                def get_webhook_info(self):
                    return dict(url="")

                def get_updates(self, offset=None, timeout=30):
                    if stop.wait(0.02):
                        raise KeyboardInterrupt()
                    listening.set()
                    return [u for u in updates if offset is None or u["update_id"] >= offset]

                def send_chat_action(self, chat_id, action="typing"):
                    actions.append((chat_id, action))
                    typing.set()
                    return True

                def send_message(self, chat_id, text, **kwargs):
                    sent.append((chat_id, text))
                    replied.set()
                    return dict(message_id=100)

            class SlowBrain:
                def run(self, context):
                    started.set()
                    if not release.wait(6):
                        raise RuntimeError("Test did not release the model")
                    event = context["events"][0]
                    with Store(home, run_id=context["run"]["id"]) as store:
                        store.message_send("david", "The employee's own answer", key="answer",
                                           thread=event["thread"], in_reply_to=event["id"])
                    return BrainResult("Reply queued")

            adapter = TelegramAdapter(str(config))

            def run_adapter():
                try:
                    adapter.run()
                except BaseException as error:
                    errors.append(error)

            def run_employee():
                try:
                    Engine(home, SlowBrain()).run()
                except BaseException as error:
                    errors.append(error)

            with patch.object(adapter, "_make_client", return_value=Provider()):
                transport = threading.Thread(target=run_adapter, daemon=True)
                employee = threading.Thread(target=run_employee, daemon=True)
                transport.start()
                try:
                    self.assertTrue(listening.wait(2), "Adapter did not begin polling")
                    updates.append(dict(update_id=1, message=dict(message_id=10, date=int(time.time()),
                                   text="Investigate this", chat=dict(id=42, type="private"),
                                   **{"from": dict(id=42, is_bot=False)})))
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        with Store(home) as store:
                            if store.event_pending():
                                break
                        time.sleep(0.01)
                    else:
                        self.fail("Authenticated input did not reach the inbox")
                    employee.start()
                    self.assertTrue(started.wait(2), "Employee did not start handling input")
                    self.assertTrue(typing.wait(1.5), "A working employee gave no visible activity before its reply")
                    self.assertEqual(sent, [], "Activity must not fabricate an employee reply")
                    self.assertEqual(actions[0], (42, "typing"))
                    release.set()
                    self.assertTrue(replied.wait(2), "Employee reply was not delivered")
                finally:
                    release.set()
                    if employee.ident is not None:
                        employee.join(3)
                    stop.set()
                    transport.join(3)
                self.assertFalse(transport.is_alive())
                self.assertFalse(employee.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(sent, [(42, "The employee's own answer")])
                with Store(home) as store:
                    messages = store.outbox_list()
                    self.assertEqual(len(messages), 1)
                    self.assertEqual(store.event_pending(), [])
                receipt = json.loads(state_path.read_text())["deliveries"][messages[0]["id"]]
                self.assertEqual(receipt["status"], "accepted")


class TelegramActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "employee"
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        identity = self.store.initialize("Aster", "Colleague")
        onboarding = self.store.run_begin()
        self.store.run_finish(onboarding["id"], "Fixture onboarding complete")
        self.worker = WorkerLock(self.home)
        self.worker.__enter__()
        self.addCleanup(self.worker.__exit__, None, None, None)
        self.event = self.store.feed("david", "Investigate", source="telegram:123", thread="conversation")
        self.run = self.store.run_begin()
        state = TelegramChannelState(self.home / "state.json")
        state.employee_id = identity["id"]
        state.bot_id = 123
        state.paired = True
        state.bound_chat_id = 42
        state.bound_user_id = 42
        state.bound_colleague_id = "david"
        state.bound_local_thread = "conversation"
        state.save()
        config = self.root / "config.json"
        config.write_text(json.dumps(dict(token_file=str(self.root / "unused-token"),
                                          employee_home=str(self.home), state_file=str(state.path),
                                          colleague_id="david", local_thread="conversation")))
        self.adapter = TelegramAdapter(str(config))
        self.client = Mock()

    def service(self, now=100):
        with patch("synthetics.telegram.time.monotonic", return_value=now):
            self.adapter._service_activity(self.client, self.store)

    def replace_input(self, **overrides):
        if self.store.run_get(self.run["id"])["status"] == "running":
            self.store.run_finish(self.run["id"], "Fixture finished prior input")
        event = dict(sender="david", body="New input", source="telegram:123", thread="conversation")
        event.update(overrides)
        self.event = self.store.feed(**event)
        self.run = self.store.run_begin()

    def test_refresh_is_throttled_and_does_not_write_durable_state(self):
        before = self.adapter.state.path.read_bytes()
        audit_count = self.store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        for now in (100, 100.1, 103.99, 104, 108):
            self.service(now)
        self.assertEqual(self.client.send_chat_action.call_count, 3)
        self.client.send_chat_action.assert_called_with(42, "typing")
        self.assertEqual(self.adapter.state.path.read_bytes(), before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], audit_count)
        self.assertEqual(self.store.outbox_list(), [])

    def test_dead_worker_with_stale_active_run_does_not_show_typing(self):
        self.worker.__exit__(None, None, None)
        self.service()
        self.client.send_chat_action.assert_not_called()

    def test_replacement_worker_does_not_animate_prior_lifetime_run(self):
        started = datetime.fromisoformat(self.run["started_at"].replace("Z", "+00:00")).timestamp()
        os.utime(self.home / "worker.lock", (started + 1, started + 1))
        self.service()
        self.client.send_chat_action.assert_not_called()

    def test_finished_or_failed_run_stops_refreshing(self):
        self.service()
        self.store.run_finish(self.run["id"], "Done")
        self.service(104)
        self.assertEqual(self.client.send_chat_action.call_count, 1)
        self.replace_input()
        self.store.run_fail(self.run["id"], "Failed")
        self.service(108)
        self.assertEqual(self.client.send_chat_action.call_count, 1)

    def test_only_bound_colleague_bot_source_and_thread_activate_typing(self):
        for override in ({"sender": "other"}, {"source": "manual"},
                         {"source": "telegram:999"}, {"thread": "other-thread"}):
            with self.subTest(override=override):
                self.replace_input(**override)
                self.service()
                self.client.send_chat_action.assert_not_called()

    def test_later_input_does_not_animate_a_run_that_did_not_capture_it(self):
        self.store.run_finish(self.run["id"], "Done")
        self.run = self.store.run_begin()
        self.store.feed("david", "Arrived later", source="telegram:123", thread="conversation")
        self.service()
        self.client.send_chat_action.assert_not_called()

    def test_unpaired_channel_never_shows_typing(self):
        self.adapter.state.paired = False
        self.service()
        self.client.send_chat_action.assert_not_called()

    def test_old_uncorrelated_reply_does_not_suppress_new_input(self):
        self.store.message_send("david", "Old answer", thread="conversation", key="old")
        self.replace_input()
        self.service()
        self.client.send_chat_action.assert_called_once_with(42, "typing")

    def test_unrelated_reply_metadata_and_other_routes_do_not_suppress_typing(self):
        self.store.message_send("david", "Other answer", thread="conversation", key="other-event",
                                in_reply_to="an-earlier-event")
        self.store.message_send("other", "Other colleague", thread="conversation", key="other-person")
        self.store.message_send("david", "Other thread", thread="elsewhere", key="other-thread")
        self.service()
        self.client.send_chat_action.assert_called_once_with(42, "typing")

    def test_current_uncorrelated_reply_suppresses_typing(self):
        self.store.message_send("david", "Answer", thread="conversation", key="answer")
        self.service()
        self.client.send_chat_action.assert_not_called()

    def test_reply_without_optional_thread_metadata_suppresses_typing(self):
        for index, correlated in enumerate((False, True)):
            with self.subTest(correlated=correlated):
                self.replace_input()
                self.client.reset_mock()
                self.store.message_send("david", "Answer on the default route", key="answer-%s" % index,
                                        in_reply_to=self.event["id"] if correlated else None)
                self.service(100 + index * CHAT_ACTION_INTERVAL)
                self.client.send_chat_action.assert_not_called()

    def test_correlated_reply_from_interrupted_run_suppresses_replay_typing(self):
        self.store.message_send("david", "Answer", thread="conversation", key="answer", in_reply_to=self.event["id"])
        self.store.run_fail(self.run["id"], "Interrupted after queueing reply")
        self.run = self.store.run_begin()
        self.service()
        self.client.send_chat_action.assert_not_called()

    def test_batch_keeps_typing_for_unanswered_captured_message(self):
        self.store.run_finish(self.run["id"], "Done")
        first = self.store.feed("david", "First", source="telegram:123", thread="conversation")
        self.store.feed("david", "Second", source="telegram:123", thread="conversation")
        self.run = self.store.run_begin()
        self.store.message_send("david", "First answer", thread="conversation", key="first", in_reply_to=first["id"])
        self.service()
        self.client.send_chat_action.assert_called_once_with(42, "typing")

    def test_rate_limit_cooldown_is_bounded_and_nonfatal(self):
        self.client.send_chat_action.side_effect = [TelegramAPIError("Slow down", error_code=429, retry_after=999), True]
        with self.assertLogs("synthetics.telegram", level="WARNING"):
            self.service(100)
        self.service(159.99)
        self.assertEqual(self.client.send_chat_action.call_count, 1)
        self.service(160)
        self.assertEqual(self.client.send_chat_action.call_count, 2)

    def test_malformed_rate_limit_value_is_nonfatal_with_normal_cooldown(self):
        self.client.send_chat_action.side_effect = [TelegramAPIError("Slow down", error_code=429, retry_after="invalid"), True]
        with self.assertLogs("synthetics.telegram", level="WARNING"):
            self.service(100)
        self.service(103.99)
        self.assertEqual(self.client.send_chat_action.call_count, 1)
        self.service(104)
        self.assertEqual(self.client.send_chat_action.call_count, 2)

    def test_network_error_preserves_outbox_service_during_inbound_backoff(self):
        self.client.send_chat_action.side_effect = TelegramNetworkError("Unavailable")
        self.client.send_message.return_value = dict(message_id=100)
        outbox = OutboxDelivery(self.adapter.state, "david", self.client, local_thread="conversation")
        outbox.establish_baseline(self.store)
        clock = {"now": 100.0, "queued": False}

        def sleep(seconds):
            clock["now"] += seconds
            if clock["now"] >= 102 and not clock["queued"]:
                self.store.message_send("david", "Actual answer", thread="conversation", key="answer")
                clock["queued"] = True

        with patch("synthetics.telegram.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("synthetics.telegram.time.sleep", side_effect=sleep), \
                self.assertLogs("synthetics.telegram", level="WARNING"):
            self.adapter._sleep_servicing_outbox(8, outbox, self.store, activity_client=self.client)
        self.assertEqual(self.client.send_chat_action.call_count, 1)
        self.client.send_message.assert_called_once()
        message = self.store.outbox_list()[0]
        self.assertEqual(self.adapter.state.get_delivery(message["id"])["status"], "accepted")
        self.assertIsNone(self.adapter.state.update_cursor)

    def test_api_client_uses_short_timeout_and_correct_method(self):
        client = TelegramClient("fake-test-token")
        with patch.object(client, "_call", return_value=True) as call:
            self.assertTrue(client.send_chat_action(42))
        call.assert_called_once_with("sendChatAction", {"chat_id": 42, "action": "typing"}, timeout=CHAT_ACTION_TIMEOUT)
        self.assertLessEqual(CHAT_ACTION_TIMEOUT, 2)
        self.assertLess(CHAT_ACTION_INTERVAL, 5)


if __name__ == "__main__":
    unittest.main()
