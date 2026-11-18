"""
Tests for synthetics/telegram.py

Coverage:
- split_text / redact_token utilities
- TelegramChannelState persistence and properties, guard file, corrupt/missing state
- TelegramClient: success, API errors, network errors, token redaction, retry_after
- UpdateProcessor: private/group/bot filtering, pairing, text, reply correlation,
  unsupported attachments, closed-question fallback, durable pairing rejections
- OutboxDelivery: baseline, historical skip, delivery, question mapping, split,
  uncertain/failed states, in-flight recovery, thread filtering,
  per-part persistence, all-parts question mapping, first-send uncertainty,
  per-chat pacing, 429 retry with retry_after, 429 exhausted → uncertain,
  accepted parts preserved after 429, rebuild_question_map
- TelegramAdapter startup: bot identity binding/mismatch, webhook rejection,
  webhook URL not in error, employee identity binding/mismatch,
  colleague_id binding/mismatch, local_thread binding/mismatch
- Batch cursor: batch stops at first store_error (no hole)
- Restart and deduplication: cursor persistence, store-level idempotency,
  cursor not advanced on store_error, employee_id binding with real Store
- Baseline ordering: baseline established before pairing wakeup feed
- _feed_event direct call

Fake HTTP server (FakeTelegramServer) intercepts Bot API calls so no real network
activity occurs. MockStore provides a test double for the employee Store where a
real SQLite database is not needed; it also supports closed-question simulation.
Real Store used for idempotency, identity binding, and question-mapping tests.
"""

import http.server
import json
import queue
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

from synthetics.store import Store
from synthetics.telegram import (
    MAX_MESSAGE_LENGTH,
    PART_SEND_INTERVAL,
    RATE_LIMIT_RETRIES,
    OutboxDelivery,
    TelegramAPIError,
    TelegramAdapter,
    TelegramChannelState,
    TelegramClient,
    TelegramConfig,
    TelegramNetworkError,
    UpdateProcessor,
    _feed_event,
    redact_token,
    split_text,
)


# ---------------------------------------------------------------------------
# Fake HTTP server
# ---------------------------------------------------------------------------

class _FakeHandler(http.server.BaseHTTPRequestHandler):
    """Route both GET and POST to the response queue for the extracted method."""

    def log_message(self, fmt, *args):  # silence server output
        pass

    def _respond(self):
        # Path: /botTOKEN/METHOD  or just /METHOD
        raw_path = self.path.split("?")[0]
        method_name = raw_path.strip("/").split("/")[-1]

        try:
            response = self.server.response_queues[method_name].get_nowait()
        except (KeyError, queue.Empty):
            response = {
                "ok": False,
                "error_code": 400,
                "description": f"[FakeTelegramServer] no response queued for {method_name!r}",
            }

        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length) if length else b""
        self.server.requests.append({"method": method_name, "body": body_bytes})

        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    do_GET = _respond
    do_POST = _respond


class FakeTelegramServer:
    """
    Minimal fake Telegram Bot API server for tests.

    Usage::

        srv = FakeTelegramServer()
        srv.start()
        srv.enqueue("getMe", {"ok": True, "result": {"id": 1, "username": "bot"}})
        client = TelegramClient("token", api_base=srv.api_base)
        me = client.get_me()   # hits the fake server
        srv.stop()
    """

    def __init__(self):
        self.response_queues: dict = {}
        self.requests: list = []
        self._server = http.server.HTTPServer(("127.0.0.1", 0), _FakeHandler)
        self._server.requests = self.requests
        self._server.response_queues = self.response_queues
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def enqueue(self, method: str, response: dict) -> None:
        self.response_queues.setdefault(method, queue.Queue()).put(response)

    def get_requests(self, method: str = None) -> list:
        if method:
            return [r for r in self.requests if r["method"] == method]
        return list(self.requests)


# ---------------------------------------------------------------------------
# Mock store
# ---------------------------------------------------------------------------

class MockStore:
    """
    Test double for the employee Store.  Captures feed() calls; serves a
    configurable outbox list.  Supports in_reply_to, identity(), and
    closed-question simulation.
    """

    EMPLOYEE_ID = "employee-test-001"

    def __init__(self, outbox=None, closed_questions=None):
        self._outbox = list(outbox or [])
        self.fed: list = []
        self._next_id = 1
        # Set of question IDs that are already answered (closed)
        self._closed_questions = set(closed_questions or [])
        # Track call order for ordering assertions
        self.call_log: list = []

    def identity(self):
        return {"id": self.EMPLOYEE_ID, "name": "Vesper"}

    def outbox_list(self):
        self.call_log.append("outbox_list")
        return list(self._outbox)

    def feed(self, sender, body, kind="message", thread=None, reply_to=None,
             source="manual", external_id=None, occurred_at=None,
             in_reply_to=None):
        self.call_log.append("feed")
        if reply_to and reply_to in self._closed_questions:
            raise ValueError(
                "Reply must come from the question's recipient and answer an open question."
            )
        event = {
            "id": f"event-{self._next_id:04d}",
            "sender": sender,
            "body": body,
            "kind": kind,
            "thread": thread,
            "reply_to": reply_to,
            "in_reply_to": in_reply_to,
            "source": source,
            "external_id": external_id,
            "occurred_at": occurred_at,
        }
        self._next_id += 1
        self.fed.append(event)
        return event


# ---------------------------------------------------------------------------
# Utility: build a Telegram update dict
# ---------------------------------------------------------------------------

def _msg_update(update_id, text, user_id=42, chat_id=42, is_bot=False,
                chat_type="private", date=1700000000,
                reply_to_message_id=None, attachment=None):
    from_user = {"id": user_id, "is_bot": is_bot, "first_name": "Dave"}
    msg: dict = {
        "message_id": update_id * 10,
        "from": from_user,
        "chat": {"id": chat_id, "type": chat_type},
        "date": date,
    }
    if text is not None:
        msg["text"] = text
    if reply_to_message_id is not None:
        msg["reply_to_message"] = {"message_id": reply_to_message_id}
    if attachment:
        atype, data = attachment
        msg[atype] = data
    return {"update_id": update_id, "message": msg}


def _paired_state(tmp_dir, user_id=42, chat_id=42, bot_id=99):
    path = Path(tmp_dir) / "state.json"
    state = TelegramChannelState(path)
    state.bot_id = bot_id
    state.bot_username = "testbot"
    state.paired = True
    state.bound_user_id = user_id
    state.bound_chat_id = chat_id
    state.outbox_baseline_at = ""  # empty baseline → deliver all future msgs
    state.save()
    return state


# ---------------------------------------------------------------------------
# split_text
# ---------------------------------------------------------------------------

class TestSplitText(unittest.TestCase):

    def test_short_text_unchanged(self):
        self.assertEqual(split_text("hello"), ["hello"])

    def test_exact_max_length_unchanged(self):
        text = "x" * MAX_MESSAGE_LENGTH
        self.assertEqual(split_text(text), [text])

    def test_over_limit_produces_two_parts(self):
        text = "a" * MAX_MESSAGE_LENGTH + " extra"
        parts = split_text(text)
        self.assertEqual(len(parts), 2)
        self.assertLessEqual(max(len(p) for p in parts), MAX_MESSAGE_LENGTH)

    def test_split_prefers_newline_break(self):
        half = "x" * (MAX_MESSAGE_LENGTH - 1)
        text = half + "\n" + half
        parts = split_text(text)
        self.assertEqual(len(parts), 2)
        # First part ends at the newline position
        self.assertTrue(parts[0].endswith("x"))

    def test_split_falls_back_to_space(self):
        half = "x" * (MAX_MESSAGE_LENGTH - 1)
        text = half + " " + half
        parts = split_text(text)
        self.assertEqual(len(parts), 2)

    def test_split_hard_cuts_no_whitespace(self):
        text = "a" * (MAX_MESSAGE_LENGTH + 1)
        parts = split_text(text)
        self.assertEqual(len(parts), 2)
        self.assertLessEqual(max(len(p) for p in parts), MAX_MESSAGE_LENGTH)


# ---------------------------------------------------------------------------
# redact_token
# ---------------------------------------------------------------------------

class TestRedactToken(unittest.TestCase):

    def test_token_present_is_replaced(self):
        result = redact_token("error calling /botSECRET/getMe", "SECRET")
        self.assertNotIn("SECRET", result)
        self.assertIn("[REDACTED]", result)

    def test_token_absent_unchanged(self):
        result = redact_token("some error", "SECRET")
        self.assertEqual(result, "some error")

    def test_empty_token_unchanged(self):
        result = redact_token("url with  in it", "")
        self.assertEqual(result, "url with  in it")


# ---------------------------------------------------------------------------
# TelegramChannelState
# ---------------------------------------------------------------------------

class TestTelegramChannelState(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_state_has_defaults(self):
        state = TelegramChannelState(self.path)
        self.assertIsNone(state.bot_id)
        self.assertFalse(state.paired)
        self.assertIsNone(state.update_cursor)
        self.assertIsNone(state.outbox_baseline_at)
        self.assertEqual(state.deliveries, {})
        self.assertEqual(state.question_map, {})
        self.assertIsNone(state.employee_id)
        self.assertIsNone(state.bound_colleague_id)
        self.assertIsNone(state.bound_local_thread)

    def test_save_and_reload(self):
        state = TelegramChannelState(self.path)
        state.bot_id = 7
        state.bot_username = "mybot"
        state.paired = True
        state.bound_user_id = 100
        state.bound_chat_id = 200
        state.update_cursor = 55
        state.employee_id = "employee-abc"
        state.bound_colleague_id = "david"
        state.bound_local_thread = "codex:conv-001"
        state.save()

        state2 = TelegramChannelState(self.path)
        self.assertEqual(state2.bot_id, 7)
        self.assertEqual(state2.bot_username, "mybot")
        self.assertTrue(state2.paired)
        self.assertEqual(state2.bound_user_id, 100)
        self.assertEqual(state2.bound_chat_id, 200)
        self.assertEqual(state2.update_cursor, 55)
        self.assertEqual(state2.employee_id, "employee-abc")
        self.assertEqual(state2.bound_colleague_id, "david")
        self.assertEqual(state2.bound_local_thread, "codex:conv-001")

    def test_state_file_is_mode_600(self):
        state = TelegramChannelState(self.path)
        state.save()
        import stat
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_set_delivery_persisted(self):
        state = TelegramChannelState(self.path)
        state.set_delivery("msg-1", {"status": "accepted", "telegram_message_ids": [9]})
        state2 = TelegramChannelState(self.path)
        self.assertEqual(state2.get_delivery("msg-1")["status"], "accepted")

    def test_set_delivery_with_question_persists_both(self):
        """set_delivery_with_question persists delivery record and question map atomically."""
        state = TelegramChannelState(self.path)
        record = {"status": "in-flight", "telegram_message_ids": [101]}
        state.set_delivery_with_question("msg-q", record,
                                         tg_question_id=101, question_id="question-X")
        state2 = TelegramChannelState(self.path)
        self.assertEqual(state2.get_delivery("msg-q")["telegram_message_ids"], [101])
        self.assertEqual(state2.resolve_question(101), "question-X")

    def test_add_and_resolve_question_mapping(self):
        state = TelegramChannelState(self.path)
        state.add_question_mapping(1234, "question-abc")
        self.assertEqual(state.resolve_question(1234), "question-abc")
        self.assertIsNone(state.resolve_question(9999))

    def test_question_map_persisted(self):
        state = TelegramChannelState(self.path)
        state.add_question_mapping(77, "question-xyz")
        state2 = TelegramChannelState(self.path)
        self.assertEqual(state2.resolve_question(77), "question-xyz")

    def test_record_rejection_persisted(self):
        state = TelegramChannelState(self.path)
        state.record_rejection("wrong_nonce", 999)
        state2 = TelegramChannelState(self.path)
        rejections = state2._d.get("pairing_rejections", [])
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["reason"], "wrong_nonce")
        self.assertEqual(rejections[0]["update_id"], 999)

    def test_record_rejection_multiple_accumulate(self):
        state = TelegramChannelState(self.path)
        state.record_rejection("wrong_nonce", 1)
        state.record_rejection("no_pending_nonce", 2)
        state2 = TelegramChannelState(self.path)
        rejections = state2._d.get("pairing_rejections", [])
        self.assertEqual(len(rejections), 2)

    # ---- guard file and corrupt/missing state ----

    def test_guard_file_written_on_first_save(self):
        """A .guard marker file is written alongside the state file on first save."""
        state = TelegramChannelState(self.path)
        self.assertFalse(state._guard_path().exists())
        state.save()
        self.assertTrue(state._guard_path().exists())

    def test_missing_state_with_guard_raises(self):
        """If the state file is gone but guard exists, loading raises RuntimeError."""
        state = TelegramChannelState(self.path)
        state.bot_id = 42
        state.save()
        # Simulate state file loss (guard still present)
        self.path.unlink()
        with self.assertRaises(RuntimeError) as ctx:
            TelegramChannelState(self.path)
        self.assertIn("missing", str(ctx.exception).lower())
        self.assertIn("guard", str(ctx.exception).lower())

    def test_no_state_no_guard_is_fresh_start(self):
        """Absent both state and guard is a valid fresh start."""
        state = TelegramChannelState(self.path)
        self.assertFalse(state.paired)
        self.assertIsNone(state.bot_id)

    def test_corrupt_state_file_raises_on_load(self):
        """A corrupt (unparseable JSON) state file raises RuntimeError."""
        self.path.write_text("{invalid json!!!", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            TelegramChannelState(self.path)
        self.assertIn("corrupt", str(ctx.exception).lower())

    def test_empty_state_file_raises_on_load(self):
        """An empty state file (truncated write) also raises RuntimeError."""
        self.path.write_text("", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            TelegramChannelState(self.path)
        self.assertIn("corrupt", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# TelegramClient (HTTP level, using FakeTelegramServer)
# ---------------------------------------------------------------------------

class TestTelegramClient(unittest.TestCase):

    def setUp(self):
        self.srv = FakeTelegramServer()
        self.srv.start()
        self.token = "12345:TestToken"
        self.client = TelegramClient(self.token, api_base=self.srv.api_base)

    def tearDown(self):
        self.srv.stop()

    def test_get_me_success(self):
        self.srv.enqueue("getMe", {
            "ok": True, "result": {"id": 1, "username": "mybot", "is_bot": True}
        })
        me = self.client.get_me()
        self.assertEqual(me["id"], 1)
        self.assertEqual(me["username"], "mybot")

    def test_get_me_api_error_raises_telegram_api_error(self):
        self.srv.enqueue("getMe", {
            "ok": False, "error_code": 401, "description": "Unauthorized"
        })
        with self.assertRaises(TelegramAPIError) as ctx:
            self.client.get_me()
        self.assertEqual(ctx.exception.error_code, 401)

    def test_token_not_in_api_error_message(self):
        self.srv.enqueue("getMe", {
            "ok": False, "error_code": 401,
            "description": f"Bad token 12345:TestToken in request"
        })
        with self.assertRaises(TelegramAPIError) as ctx:
            self.client.get_me()
        self.assertNotIn(self.token, str(ctx.exception))

    def test_get_webhook_info_no_url(self):
        self.srv.enqueue("getWebhookInfo", {
            "ok": True, "result": {"url": "", "has_custom_certificate": False}
        })
        info = self.client.get_webhook_info()
        self.assertEqual(info["url"], "")

    def test_get_updates_sends_offset_and_timeout(self):
        self.srv.enqueue("getUpdates", {"ok": True, "result": []})
        updates = self.client.get_updates(offset=10, timeout=5)
        self.assertEqual(updates, [])
        req = self.srv.get_requests("getUpdates")[0]
        body = urllib.parse.parse_qs(req["body"].decode())
        self.assertEqual(body["offset"], ["10"])
        self.assertEqual(body["timeout"], ["5"])

    def test_send_message_success(self):
        self.srv.enqueue("sendMessage", {
            "ok": True, "result": {"message_id": 55, "text": "hello"}
        })
        result = self.client.send_message(42, "hello")
        self.assertEqual(result["message_id"], 55)

    def test_send_message_with_reply_to(self):
        self.srv.enqueue("sendMessage", {
            "ok": True, "result": {"message_id": 56, "text": "reply"}
        })
        self.client.send_message(42, "reply", reply_to_message_id=100)
        req = self.srv.get_requests("sendMessage")[0]
        body = urllib.parse.parse_qs(req["body"].decode())
        self.assertEqual(body["reply_to_message_id"], ["100"])

    def test_token_not_in_network_error(self):
        # Point client at a port with nothing listening
        client = TelegramClient(self.token, api_base="http://127.0.0.1:1")
        with self.assertRaises(TelegramNetworkError) as ctx:
            client.get_me()
        self.assertNotIn(self.token, str(ctx.exception))

    def test_rate_limit_error_carries_retry_after(self):
        """429 response with parameters.retry_after populates TelegramAPIError.retry_after."""
        self.srv.enqueue("sendMessage", {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 15",
            "parameters": {"retry_after": 15},
        })
        with self.assertRaises(TelegramAPIError) as ctx:
            self.client.send_message(42, "hi")
        self.assertEqual(ctx.exception.error_code, 429)
        self.assertEqual(ctx.exception.retry_after, 15)

    def test_non_rate_limit_error_has_no_retry_after(self):
        """Non-429 errors have retry_after=None."""
        self.srv.enqueue("sendMessage", {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request",
        })
        with self.assertRaises(TelegramAPIError) as ctx:
            self.client.send_message(42, "hi")
        self.assertEqual(ctx.exception.error_code, 400)
        self.assertIsNone(ctx.exception.retry_after)


# ---------------------------------------------------------------------------
# UpdateProcessor
# ---------------------------------------------------------------------------

class TestUpdateProcessor(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = _paired_state(self.tmp.name, user_id=42, chat_id=42, bot_id=99)
        self.store = MockStore()
        self.proc = UpdateProcessor(self.state, "david")

    def tearDown(self):
        self.tmp.cleanup()

    # ---- rejection ----

    def test_non_private_chat_rejected(self):
        upd = _msg_update(1, "hello", user_id=42, chat_id=100, chat_type="group")
        result = self.proc.process(upd, self.store)
        self.assertFalse(result["processed"])
        self.assertEqual(result["reason"], "non_private_chat")
        self.assertEqual(self.store.fed, [])

    def test_bot_sender_rejected(self):
        upd = _msg_update(2, "hello", user_id=42, is_bot=True)
        result = self.proc.process(upd, self.store)
        self.assertFalse(result["processed"])
        self.assertEqual(result["reason"], "bot_sender")
        self.assertEqual(self.store.fed, [])

    def test_unrecognized_sender_rejected(self):
        upd = _msg_update(3, "hello", user_id=999)  # wrong user
        result = self.proc.process(upd, self.store)
        self.assertFalse(result["processed"])
        self.assertEqual(result["reason"], "unrecognized_sender")
        self.assertEqual(self.store.fed, [])

    def test_no_message_field_skipped(self):
        result = self.proc.process({"update_id": 4}, self.store)
        self.assertFalse(result["processed"])
        self.assertEqual(result["reason"], "no_message_field")

    # ---- text messages ----

    def test_text_message_fed_to_store(self):
        upd = _msg_update(5, "Hello from David", user_id=42)
        result = self.proc.process(upd, self.store)
        self.assertTrue(result["processed"])
        self.assertEqual(result["reason"], "text")
        self.assertEqual(len(self.store.fed), 1)
        ev = self.store.fed[0]
        self.assertEqual(ev["body"], "Hello from David")
        self.assertEqual(ev["sender"], "david")
        self.assertEqual(ev["thread"], "telegram:42")
        self.assertIsNone(ev["reply_to"])

    def test_text_message_external_id_includes_bot_and_update_id(self):
        upd = _msg_update(7, "hi", user_id=42)
        self.proc.process(upd, self.store)
        self.assertEqual(self.store.fed[0]["external_id"], "tg:99:7")

    def test_occurred_at_from_telegram_date(self):
        upd = _msg_update(8, "ts test", user_id=42, date=1700000000)
        self.proc.process(upd, self.store)
        occurred = self.store.fed[0]["occurred_at"]
        self.assertIn("2023", occurred)  # 1700000000 is in 2023

    # ---- reply correlation ----

    def test_reply_to_known_question_correlated(self):
        # Map telegram message_id=100 → question-Q1
        self.state.add_question_mapping(100, "question-Q1")
        upd = _msg_update(9, "my answer", user_id=42, reply_to_message_id=100)
        result = self.proc.process(upd, self.store)
        self.assertTrue(result["processed"])
        self.assertEqual(self.store.fed[0]["reply_to"], "question-Q1")

    def test_reply_to_unknown_message_has_no_reply_to(self):
        upd = _msg_update(10, "reply to unknown", user_id=42, reply_to_message_id=999)
        self.proc.process(upd, self.store)
        self.assertIsNone(self.store.fed[0]["reply_to"])

    def test_plain_message_has_no_reply_to(self):
        upd = _msg_update(11, "plain text", user_id=42)
        self.proc.process(upd, self.store)
        self.assertIsNone(self.store.fed[0]["reply_to"])

    def test_reply_preserves_provider_in_reply_to(self):
        """Generic in_reply_to (provider message ID) is preserved even without question map."""
        upd = _msg_update(15, "just a reply", user_id=42, reply_to_message_id=555)
        self.proc.process(upd, self.store)
        # Provider reply message ID should be in in_reply_to
        ev = self.store.fed[0]
        self.assertEqual(ev.get("in_reply_to"), "555")

    def test_closed_question_falls_back_to_plain_message(self):
        """When store.feed raises ValueError for a closed question, retry as plain message."""
        self.state.add_question_mapping(200, "question-CLOSED")
        store = MockStore(closed_questions={"question-CLOSED"})
        proc = UpdateProcessor(self.state, "david")

        upd = _msg_update(16, "late reply", user_id=42, reply_to_message_id=200)
        result = proc.process(upd, store)
        # Should still succeed as a plain message
        self.assertTrue(result["processed"])
        self.assertEqual(len(store.fed), 1)
        # reply_to must be None (closed question not correlated)
        self.assertIsNone(store.fed[0]["reply_to"])

    def test_store_error_returns_not_processed(self):
        """A generic store error returns store_error reason (cursor must not advance)."""
        class BrokenStore:
            def feed(self, *args, **kwargs):
                raise OSError("disk full")
            def identity(self):
                return {"id": "employee-test"}

        proc = UpdateProcessor(self.state, "david")
        upd = _msg_update(17, "test", user_id=42)
        result = proc.process(upd, BrokenStore())
        self.assertFalse(result["processed"])
        self.assertEqual(result["reason"], "store_error")

    # ---- unsupported attachments ----

    def test_unsupported_photo_recorded_explicitly(self):
        upd = _msg_update(12, None, user_id=42,
                          attachment=("photo", [{"file_id": "x", "width": 100, "height": 100}]))
        result = self.proc.process(upd, self.store)
        self.assertTrue(result["processed"])
        self.assertIn("unsupported", result["reason"])
        body = self.store.fed[0]["body"]
        self.assertIn("[Unsupported attachment: photo]", body)

    def test_unsupported_sticker_recorded(self):
        upd = _msg_update(13, None, user_id=42,
                          attachment=("sticker", {"file_id": "s"}))
        self.proc.process(upd, self.store)
        self.assertIn("sticker", self.store.fed[0]["body"])

    def test_unsupported_attachment_with_caption(self):
        upd = _msg_update(14, None, user_id=42,
                          attachment=("document", {"file_id": "d"}))
        # Manually add caption to the update's message
        upd["message"]["caption"] = "see attached"
        self.proc.process(upd, self.store)
        self.assertIn("see attached", self.store.fed[0]["body"])

    # ---- pairing ----

    def test_pairing_success(self):
        # Fresh unpaired state
        path = Path(self.tmp.name) / "pair_state.json"
        state = TelegramChannelState(path)
        state.bot_id = 99
        state.bot_username = "testbot"
        state.pairing_nonce = "TESTNONCE"
        state.save()
        proc = UpdateProcessor(state, "david")

        upd = _msg_update(20, "/start TESTNONCE", user_id=77, chat_id=88)
        result = proc.process(upd, MockStore())
        self.assertTrue(result["processed"])
        self.assertIn(result["reason"], ("paired", "paired_no_feed"))
        # State updated
        state2 = TelegramChannelState(path)
        self.assertTrue(state2.paired)
        self.assertEqual(state2.bound_user_id, 77)
        self.assertEqual(state2.bound_chat_id, 88)
        self.assertIsNone(state2.pairing_nonce)   # consumed

    def test_pairing_wrong_nonce_rejected(self):
        path = Path(self.tmp.name) / "pair_state2.json"
        state = TelegramChannelState(path)
        state.bot_id = 99
        state.pairing_nonce = "CORRECT"
        state.save()
        proc = UpdateProcessor(state, "david")

        upd = _msg_update(21, "/start WRONG", user_id=77, chat_id=88)
        result = proc.process(upd, MockStore())
        self.assertFalse(result["processed"])
        self.assertEqual(result["reason"], "wrong_nonce")
        # Not paired
        state2 = TelegramChannelState(path)
        self.assertFalse(state2.paired)

    def test_pairing_no_pending_nonce_ignored(self):
        path = Path(self.tmp.name) / "pair_state3.json"
        state = TelegramChannelState(path)
        state.bot_id = 99
        # No nonce set
        state.save()
        proc = UpdateProcessor(state, "david")

        upd = _msg_update(22, "/start ANYTHING", user_id=77, chat_id=88)
        result = proc.process(upd, MockStore())
        self.assertFalse(result["processed"])
        self.assertEqual(result["reason"], "no_pending_nonce")

    def test_pairing_wrong_nonce_rejection_is_durable(self):
        """wrong_nonce rejection is persisted before cursor advance."""
        path = Path(self.tmp.name) / "pair_durable.json"
        state = TelegramChannelState(path)
        state.bot_id = 99
        state.pairing_nonce = "CORRECT"
        state.save()
        proc = UpdateProcessor(state, "david")

        upd = _msg_update(30, "/start WRONG", user_id=77, chat_id=88)
        proc.process(upd, MockStore())

        # Rejection must be durable in state
        state2 = TelegramChannelState(path)
        rejections = state2._d.get("pairing_rejections", [])
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["reason"], "wrong_nonce")

    def test_pairing_no_pending_nonce_rejection_is_durable(self):
        """no_pending_nonce rejection is persisted before cursor advance."""
        path = Path(self.tmp.name) / "pair_durable2.json"
        state = TelegramChannelState(path)
        state.bot_id = 99
        state.save()
        proc = UpdateProcessor(state, "david")

        upd = _msg_update(31, "/start ANYTHING", user_id=77)
        proc.process(upd, MockStore())

        state2 = TelegramChannelState(path)
        rejections = state2._d.get("pairing_rejections", [])
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["reason"], "no_pending_nonce")

    def test_pairing_baseline_established_before_feed(self):
        """
        Outbox baseline must be established BEFORE the pairing wakeup is fed.
        Any employee reply queued in response to the wakeup has a later timestamp
        than the baseline and must be delivered.
        """
        path = Path(self.tmp.name) / "pair_baseline.json"
        state = TelegramChannelState(path)
        state.bot_id = 99
        state.pairing_nonce = "NONCE123"
        state.save()

        # Pre-populate an outbox message (historical — should be excluded by baseline)
        historical_msg = {
            "id": "msg-hist",
            "to": "david",
            "body": "old message",
            "created_at": "2026-01-01T00:00:00.000000Z",
        }
        store = MockStore(outbox=[historical_msg])

        # OutboxDelivery will record baseline establishment
        class _MockClient:
            def send_message(self, *a, **kw):
                return {"message_id": 1}

        state_obj = TelegramChannelState(path)
        outbox_obj = OutboxDelivery(state_obj, "david", _MockClient())

        proc = UpdateProcessor(state_obj, "david")
        upd = _msg_update(40, "/start NONCE123", user_id=77, chat_id=88)
        result = proc.process(upd, store, outbox=outbox_obj)

        # After pairing, baseline should be established
        state2 = TelegramChannelState(path)
        self.assertIsNotNone(state2.outbox_baseline_at)
        # historical_msg is at or before baseline → not new
        self.assertFalse(outbox_obj._is_new(historical_msg))

    def test_local_thread_override_used_for_incoming(self):
        """When local_thread is configured, incoming messages use that thread."""
        proc = UpdateProcessor(self.state, "david", local_thread="codex:conv-001")
        upd = _msg_update(50, "hello", user_id=42)
        result = proc.process(upd, self.store)
        self.assertTrue(result["processed"])
        self.assertEqual(self.store.fed[0]["thread"], "codex:conv-001")


# ---------------------------------------------------------------------------
# OutboxDelivery
# ---------------------------------------------------------------------------

def _outbox_msg(mid, to, body, created_at="2026-01-01T00:00:00.000000Z",
                question_id=None, kind="message", thread=None):
    return {
        "id": mid,
        "to": to,
        "body": body,
        "kind": kind,
        "question_id": question_id,
        "created_at": created_at,
        "thread": thread,
    }


class _MockClient:
    """Minimal mock of TelegramClient for outbox tests."""

    def __init__(self, responses=None):
        # responses: list of return values or exceptions per sendMessage call
        self._responses = list(responses or [])
        self.calls: list = []

    def send_message(self, chat_id, text, reply_to_message_id=None):
        self.calls.append({"chat_id": chat_id, "text": text})
        if not self._responses:
            return {"message_id": len(self.calls) * 10}
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class TestOutboxDelivery(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = _paired_state(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _delivery(self, outbox=None, responses=None, local_thread=None):
        client = _MockClient(responses)
        return OutboxDelivery(self.state, "david", client,
                              local_thread=local_thread), client

    # ---- baseline ----

    def test_establish_baseline_with_messages_records_latest(self):
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "old", "2026-01-01T00:00:00.000000Z"),
            _outbox_msg("m2", "david", "new", "2026-06-01T00:00:00.000000Z"),
        ])
        self.state.outbox_baseline_at = None
        self.state.save()
        od, _ = self._delivery()
        od.establish_baseline(store)
        self.assertEqual(self.state.outbox_baseline_at, "2026-06-01T00:00:00.000000Z")

    def test_establish_baseline_empty_outbox_uses_empty_string(self):
        store = MockStore(outbox=[])
        self.state.outbox_baseline_at = None
        self.state.save()
        od, _ = self._delivery()
        od.establish_baseline(store)
        self.assertEqual(self.state.outbox_baseline_at, "")

    def test_establish_baseline_not_overwritten_if_already_set(self):
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "msg", "2026-12-01T00:00:00.000000Z"),
        ])
        self.state.outbox_baseline_at = "2026-06-01T00:00:00.000000Z"
        self.state.save()
        od, _ = self._delivery()
        od.establish_baseline(store)
        self.assertEqual(self.state.outbox_baseline_at, "2026-06-01T00:00:00.000000Z")

    # ---- historical skip ----

    def test_historical_messages_at_baseline_not_delivered(self):
        # Messages at or before baseline_at should not be sent
        self.state.outbox_baseline_at = "2026-06-01T00:00:00.000000Z"
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "before", "2026-01-01T00:00:00.000000Z"),
            _outbox_msg("m2", "david", "exact",  "2026-06-01T00:00:00.000000Z"),
        ])
        od, client = self._delivery()
        attempted = od.process_outbox(store)
        self.assertEqual(attempted, [])
        self.assertEqual(client.calls, [])

    def test_new_message_above_baseline_delivered(self):
        self.state.outbox_baseline_at = "2026-06-01T00:00:00.000000Z"
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "new!", "2026-07-01T00:00:00.000000Z"),
        ])
        od, client = self._delivery(responses=[{"message_id": 55}])
        od.process_outbox(store)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["text"], "new!")

    def test_messages_to_other_recipients_not_delivered(self):
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m1", "rowan", "not for david", "2026-07-01T00:00:00.000000Z"),
        ])
        od, client = self._delivery()
        od.process_outbox(store)
        self.assertEqual(client.calls, [])

    # ---- thread filtering ----

    def test_message_with_wrong_thread_not_delivered(self):
        """Messages explicitly routed to a different thread are not delivered."""
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "wrong channel",
                        "2026-07-01T00:00:00.000000Z",
                        thread="some-other-thread"),
        ])
        od, client = self._delivery()  # default thread = telegram:42
        od.process_outbox(store)
        self.assertEqual(client.calls, [])

    def test_message_with_matching_thread_delivered(self):
        """Messages with the private conversation thread are delivered."""
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "correct channel",
                        "2026-07-01T00:00:00.000000Z",
                        thread="telegram:42"),
        ])
        od, client = self._delivery(responses=[{"message_id": 77}])
        od.process_outbox(store)
        self.assertEqual(len(client.calls), 1)

    def test_message_with_no_thread_delivered_to_private_chat(self):
        """Messages with no explicit thread are delivered (no restriction)."""
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "no thread", "2026-07-01T00:00:00.000000Z"),
        ])
        od, client = self._delivery(responses=[{"message_id": 88}])
        od.process_outbox(store)
        self.assertEqual(len(client.calls), 1)

    def test_local_thread_override_filters_correctly(self):
        """With local_thread configured, only messages with that thread are delivered."""
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "correct", "2026-07-01T00:00:00.000000Z",
                        thread="codex:conv-001"),
            _outbox_msg("m2", "david", "wrong",   "2026-07-02T00:00:00.000000Z",
                        thread="telegram:42"),
        ])
        od, client = self._delivery(
            responses=[{"message_id": 55}],
            local_thread="codex:conv-001",
        )
        od.process_outbox(store)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["text"], "correct")

    # ---- delivery states ----

    def test_accepted_message_not_retried(self):
        self.state.outbox_baseline_at = ""
        self.state.set_delivery("m1", {"status": "accepted", "telegram_message_ids": [9]})
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "already done", "2026-07-01T00:00:00.000000Z"),
        ])
        od, client = self._delivery()
        od.process_outbox(store)
        self.assertEqual(client.calls, [])

    def test_failed_message_not_retried(self):
        self.state.outbox_baseline_at = ""
        self.state.set_delivery("m1", {"status": "failed", "error": "oops"})
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "failed before", "2026-07-01T00:00:00.000000Z"),
        ])
        od, client = self._delivery()
        od.process_outbox(store)
        self.assertEqual(client.calls, [])

    def test_uncertain_message_not_retried(self):
        self.state.outbox_baseline_at = ""
        self.state.set_delivery("m1", {"status": "uncertain", "error": "timeout"})
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "uncertain", "2026-07-01T00:00:00.000000Z"),
        ])
        od, client = self._delivery()
        od.process_outbox(store)
        self.assertEqual(client.calls, [])

    def test_successful_delivery_records_accepted_state(self):
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m1", "david", "hello", "2026-07-01T00:00:00.000000Z"),
        ])
        od, _ = self._delivery(responses=[{"message_id": 77}])
        od.process_outbox(store)
        rec = self.state.get_delivery("m1")
        self.assertEqual(rec["status"], "accepted")
        self.assertIn(77, rec["telegram_message_ids"])

    # ---- in-flight recovery ----

    def test_inflight_recovered_to_uncertain_on_restart(self):
        """In-flight records from a previous crash must become uncertain (no retry)."""
        self.state.outbox_baseline_at = ""
        self.state.set_delivery("m-crash", {
            "status": "in-flight",
            "started_at": "2026-01-01T00:00:00.000000Z",
            "telegram_message_ids": [],
        })
        store = MockStore(outbox=[
            _outbox_msg("m-crash", "david", "sent before crash",
                        "2026-07-01T00:00:00.000000Z"),
        ])
        od, client = self._delivery()
        od.recover_inflight()
        # After recovery, in-flight → uncertain
        self.assertEqual(self.state.get_delivery("m-crash")["status"], "uncertain")
        # process_outbox must not retry the now-uncertain message
        od.process_outbox(store)
        self.assertEqual(client.calls, [])

    def test_inflight_in_loop_not_retried(self):
        """In-flight messages encountered during process_outbox are not retried."""
        self.state.outbox_baseline_at = ""
        self.state.set_delivery("m-fl", {"status": "in-flight", "telegram_message_ids": []})
        store = MockStore(outbox=[
            _outbox_msg("m-fl", "david", "in-flight msg", "2026-07-01T00:00:00.000000Z"),
        ])
        od, client = self._delivery()
        od.process_outbox(store)  # no recover_inflight called
        self.assertEqual(client.calls, [])

    # ---- question mapping ----

    def test_question_outbox_adds_to_question_map(self):
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m-q1", "david", "Is the system working?",
                        "2026-07-01T00:00:00.000000Z",
                        question_id="question-Q99", kind="question"),
        ])
        od, _ = self._delivery(responses=[{"message_id": 500}])
        od.process_outbox(store)
        self.assertEqual(self.state.resolve_question(500), "question-Q99")

    def test_all_split_parts_mapped_to_question(self):
        """All returned message IDs (across split parts) are mapped to the question."""
        self.state.outbox_baseline_at = ""
        self.state.save()
        long_body = "q" * (MAX_MESSAGE_LENGTH + 100)
        store = MockStore(outbox=[
            _outbox_msg("m-qsplit", "david", long_body,
                        "2026-07-01T00:00:00.000000Z",
                        question_id="question-MULTI", kind="question"),
        ])
        od, _ = self._delivery(
            responses=[{"message_id": 101}, {"message_id": 102}]
        )
        od.process_outbox(store)
        # Both returned message IDs should be mapped
        self.assertEqual(self.state.resolve_question(101), "question-MULTI")
        self.assertEqual(self.state.resolve_question(102), "question-MULTI")

    def test_question_map_written_atomically_per_part(self):
        """
        Question mapping is written inside the send loop, alongside each accepted
        part's delivery record — not after the final accepted status write.

        The _SnapshotClient snapshots question_map at the ENTRY of each
        send_message call (before returning the message_id). Therefore:
          snapshot[0]: taken before the first send returns its message_id; the
                       code cannot yet know the ID, so the map must be empty.
          snapshot[1]: taken at the ENTRY of the second send, AFTER the code
                       processed the first return and called
                       set_delivery_with_question. Part-1's mapping (100 →
                       question-ATOMIC) must already be durable on disk here.
        This verifies that atomicity is per-part, not deferred to the end.
        """
        self.state.outbox_baseline_at = ""
        self.state.save()
        long_body = "q" * (MAX_MESSAGE_LENGTH + 100)
        store = MockStore(outbox=[
            _outbox_msg("m-atomic", "david", long_body,
                        "2026-07-01T00:00:00.000000Z",
                        question_id="question-ATOMIC", kind="question"),
        ])

        # Collect state snapshots at the ENTRY of each send_message call.
        question_map_snapshots: list = []

        class _SnapshotClient:
            def __init__(self, state):
                self._state = state
                self._calls = 0
            def send_message(self, chat_id, text, **kw):
                # Snapshot taken BEFORE returning the message_id.
                # snapshot[0]: first call entry — no ID returned yet, map empty.
                # snapshot[1]: second call entry — first mapping must be durable.
                question_map_snapshots.append(dict(self._state.question_map))
                self._calls += 1
                mid = self._calls * 100
                return {"message_id": mid}

        client = _SnapshotClient(self.state)
        od = OutboxDelivery(self.state, "david", client)
        od.process_outbox(store)

        self.assertGreater(len(question_map_snapshots), 1,
                           "Need at least 2 parts to test per-part atomicity")

        # snapshot[0]: taken before code received part-1's message_id → map is empty
        self.assertEqual(len(question_map_snapshots[0]), 0,
                         "No mapping exists before the first send_message returns")

        # snapshot[1]: taken at ENTRY of second send, after code called
        # set_delivery_with_question for part 1 (tg_id=100). Mapping must be durable.
        second_snapshot = question_map_snapshots[1]
        self.assertIn("100", second_snapshot,
                      "Part-1 mapping must be durable before second send begins")
        self.assertEqual(second_snapshot["100"], "question-ATOMIC")

    # ---- rebuild_question_map ----

    def test_rebuild_fills_missing_question_mappings(self):
        """rebuild_question_map restores mappings from delivery receipts."""
        self.state.outbox_baseline_at = ""
        # Simulate state: delivery receipt has tg IDs but question_map is empty
        self.state.set_delivery("m-rebuild", {
            "status": "accepted",
            "telegram_message_ids": [201, 202],
        })
        # Clear question_map (simulates lost mapping)
        self.state._d["question_map"] = {}
        self.state.save()

        store = MockStore(outbox=[
            _outbox_msg("m-rebuild", "david", "long msg",
                        "2026-07-01T00:00:00.000000Z",
                        question_id="question-REBUILT", kind="question"),
        ])
        od, _ = self._delivery()
        od.rebuild_question_map(store)

        # Both tg IDs should now be mapped
        self.assertEqual(self.state.resolve_question(201), "question-REBUILT")
        self.assertEqual(self.state.resolve_question(202), "question-REBUILT")

    def test_rebuild_skips_messages_without_question_id(self):
        """rebuild_question_map only processes messages with question_id."""
        self.state.outbox_baseline_at = ""
        self.state.set_delivery("m-plain", {
            "status": "accepted",
            "telegram_message_ids": [300],
        })
        store = MockStore(outbox=[
            _outbox_msg("m-plain", "david", "plain msg", "2026-07-01T00:00:00.000000Z"),
        ])
        od, _ = self._delivery()
        od.rebuild_question_map(store)
        # No mapping should be added for plain messages
        self.assertIsNone(self.state.resolve_question(300))

    # ---- split long text ----

    def test_split_long_message_calls_send_multiple_times(self):
        self.state.outbox_baseline_at = ""
        self.state.save()
        long_body = "a" * (MAX_MESSAGE_LENGTH + 100)
        store = MockStore(outbox=[
            _outbox_msg("m-long", "david", long_body, "2026-07-01T00:00:00.000000Z"),
        ])
        od, client = self._delivery(
            responses=[{"message_id": 101}, {"message_id": 102}]
        )
        od.process_outbox(store)
        self.assertEqual(len(client.calls), 2)
        for call in client.calls:
            self.assertLessEqual(len(call["text"]), MAX_MESSAGE_LENGTH)
        rec = self.state.get_delivery("m-long")
        self.assertEqual(rec["telegram_message_ids"], [101, 102])

    def test_split_parts_persisted_incrementally(self):
        """
        Each accepted part is saved to state before the next send call.
        A crash mid-send leaves the already-accepted part IDs in the record.
        """
        self.state.outbox_baseline_at = ""
        self.state.save()
        long_body = "b" * (MAX_MESSAGE_LENGTH + 100)
        store = MockStore(outbox=[
            _outbox_msg("m-persist", "david", long_body, "2026-07-01T00:00:00.000000Z"),
        ])
        # Second send fails to simulate a crash
        od, _ = self._delivery(
            responses=[
                {"message_id": 201},                      # first part ok
                TelegramNetworkError("mid-send crash"),    # second part fails
            ]
        )
        od.process_outbox(store)
        rec = self.state.get_delivery("m-persist")
        # First part ID must have been persisted before the crash
        self.assertIn(201, rec["telegram_message_ids"])
        # Status is uncertain
        self.assertEqual(rec["status"], "uncertain")

    # ---- error handling ----

    def test_api_error_marks_failed(self):
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m-fail", "david", "oops", "2026-07-01T00:00:00.000000Z"),
        ])
        od, _ = self._delivery(
            responses=[TelegramAPIError("bad request", error_code=400)]
        )
        od.process_outbox(store)
        self.assertEqual(self.state.get_delivery("m-fail")["status"], "failed")

    def test_network_error_first_send_marks_uncertain(self):
        """
        A network error (including timeout) on the VERY FIRST send is uncertain,
        not failed — Telegram may have accepted the request despite no response.
        """
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m-first-net", "david", "net fail", "2026-07-01T00:00:00.000000Z"),
        ])
        od, _ = self._delivery(
            responses=[TelegramNetworkError("connection timeout")]
        )
        od.process_outbox(store)
        # Must be uncertain, NOT failed
        self.assertEqual(self.state.get_delivery("m-first-net")["status"], "uncertain")

    def test_network_error_mid_send_marks_uncertain(self):
        """Network failure after first part of a multi-part message → uncertain."""
        self.state.outbox_baseline_at = ""
        self.state.save()
        long_body = "b" * (MAX_MESSAGE_LENGTH + 100)
        store = MockStore(outbox=[
            _outbox_msg("m-unc", "david", long_body, "2026-07-01T00:00:00.000000Z"),
        ])
        od, _ = self._delivery(
            responses=[
                {"message_id": 201},                          # first part ok
                TelegramNetworkError("timeout mid-send"),     # second part fails
            ]
        )
        od.process_outbox(store)
        rec = self.state.get_delivery("m-unc")
        self.assertEqual(rec["status"], "uncertain")
        self.assertIn(201, rec["telegram_message_ids"])

    # ---- rate limiting (429) ----

    def test_429_retried_with_retry_after(self):
        """
        A 429 response causes the adapter to honor retry_after and retry the part.
        The message should ultimately be accepted.
        """
        import unittest.mock
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m-429", "david", "hi", "2026-07-01T00:00:00.000000Z"),
        ])
        # First attempt → 429, second → accepted
        od, _ = self._delivery(
            responses=[
                TelegramAPIError("Too Many Requests", error_code=429, retry_after=1),
                {"message_id": 300},
            ]
        )
        with unittest.mock.patch("synthetics.telegram.time.sleep") as mock_sleep:
            od.process_outbox(store)
        # sleep called with the retry_after value (or capped value)
        self.assertTrue(any(call[0][0] >= 1 for call in mock_sleep.call_args_list))
        # Message ultimately accepted
        rec = self.state.get_delivery("m-429")
        self.assertEqual(rec["status"], "accepted")
        self.assertIn(300, rec["telegram_message_ids"])

    def test_429_exhausted_retries_marks_uncertain_not_failed(self):
        """After RATE_LIMIT_RETRIES+1 consecutive 429s, the delivery is uncertain."""
        import unittest.mock
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m-429-ex", "david", "hi", "2026-07-01T00:00:00.000000Z"),
        ])
        # Exhaust all retries
        responses = [
            TelegramAPIError("Too Many Requests", error_code=429, retry_after=1)
            for _ in range(RATE_LIMIT_RETRIES + 1)
        ]
        od, _ = self._delivery(responses=responses)
        with unittest.mock.patch("synthetics.telegram.time.sleep"):
            od.process_outbox(store)
        rec = self.state.get_delivery("m-429-ex")
        # Must be uncertain, NOT failed
        self.assertEqual(rec["status"], "uncertain")

    def test_non_429_api_error_still_marks_failed(self):
        """Non-429 permanent API errors are still marked as failed."""
        self.state.outbox_baseline_at = ""
        self.state.save()
        store = MockStore(outbox=[
            _outbox_msg("m-perm", "david", "hi", "2026-07-01T00:00:00.000000Z"),
        ])
        od, _ = self._delivery(
            responses=[TelegramAPIError("Forbidden", error_code=403)]
        )
        od.process_outbox(store)
        rec = self.state.get_delivery("m-perm")
        self.assertEqual(rec["status"], "failed")

    def test_accepted_parts_preserved_before_429_retry(self):
        """
        When a 429 occurs mid-split, already-accepted parts are preserved in the receipt.
        The retry re-sends only the failing part.
        """
        import unittest.mock
        self.state.outbox_baseline_at = ""
        self.state.save()
        long_body = "x" * (MAX_MESSAGE_LENGTH + 100)
        store = MockStore(outbox=[
            _outbox_msg("m-par", "david", long_body, "2026-07-01T00:00:00.000000Z"),
        ])
        # Part 1 OK, part 2 → 429 then OK
        od, _ = self._delivery(
            responses=[
                {"message_id": 401},  # part 1 OK
                TelegramAPIError("Too Many Requests", error_code=429, retry_after=1),
                {"message_id": 402},  # part 2 retry OK
            ]
        )
        with unittest.mock.patch("synthetics.telegram.time.sleep"):
            od.process_outbox(store)
        rec = self.state.get_delivery("m-par")
        self.assertEqual(rec["status"], "accepted")
        self.assertIn(401, rec["telegram_message_ids"])
        self.assertIn(402, rec["telegram_message_ids"])

    def test_pacing_between_parts(self):
        """PART_SEND_INTERVAL sleep is inserted between parts (not before the first)."""
        import unittest.mock
        self.state.outbox_baseline_at = ""
        self.state.save()
        long_body = "p" * (MAX_MESSAGE_LENGTH + 100)
        store = MockStore(outbox=[
            _outbox_msg("m-pace", "david", long_body, "2026-07-01T00:00:00.000000Z"),
        ])
        od, _ = self._delivery(
            responses=[{"message_id": 501}, {"message_id": 502}]
        )
        with unittest.mock.patch("synthetics.telegram.time.sleep") as mock_sleep:
            od.process_outbox(store)
        # sleep(PART_SEND_INTERVAL) called exactly once (between the two parts)
        pacing_calls = [c for c in mock_sleep.call_args_list
                        if c[0][0] == PART_SEND_INTERVAL]
        self.assertEqual(len(pacing_calls), 1)


# ---------------------------------------------------------------------------
# TelegramAdapter startup (bot verify / webhook check / config binding)
# ---------------------------------------------------------------------------

class TestTelegramAdapterStartup(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = FakeTelegramServer()
        self.srv.start()
        # Write a token file
        token_path = Path(self.tmp.name) / "token.txt"
        token_path.write_text("99999:TestBotToken\n")
        self.token = "99999:TestBotToken"
        # Write a minimal config
        self.config_path = Path(self.tmp.name) / "telegram.json"
        self.config_path.write_text(json.dumps({
            "token_file": str(token_path),
            "colleague_id": "david",
            "employee_home": self.tmp.name,
            "state_file": str(Path(self.tmp.name) / "state.json"),
            "lock_file": str(Path(self.tmp.name) / "telegram.lock"),
        }))

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def _adapter(self, colleague_id="david", local_thread=None):
        # Write config with specified colleague_id/local_thread
        token_path = Path(self.tmp.name) / "token.txt"
        cfg: dict = {
            "token_file": str(token_path),
            "colleague_id": colleague_id,
            "employee_home": self.tmp.name,
            "state_file": str(Path(self.tmp.name) / "state.json"),
            "lock_file": str(Path(self.tmp.name) / "telegram.lock"),
        }
        if local_thread:
            cfg["local_thread"] = local_thread
        cfg_path = Path(self.tmp.name) / "cfg_v.json"
        cfg_path.write_text(json.dumps(cfg))
        adapter = TelegramAdapter(str(cfg_path))
        adapter.state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        return adapter

    def _client(self):
        return TelegramClient(self.token, api_base=self.srv.api_base)

    def test_verify_bot_binds_on_first_run(self):
        self.srv.enqueue("getMe", {
            "ok": True, "result": {"id": 7, "username": "mybot", "is_bot": True}
        })
        adapter = self._adapter()
        client = self._client()
        adapter._verify_bot(client)
        self.assertEqual(adapter.state.bot_id, 7)
        self.assertEqual(adapter.state.bot_username, "mybot")

    def test_verify_bot_accepts_consistent_identity(self):
        state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        state.bot_id = 7
        state.save()
        self.srv.enqueue("getMe", {
            "ok": True, "result": {"id": 7, "username": "mybot", "is_bot": True}
        })
        adapter = self._adapter()
        adapter._verify_bot(self._client())   # should not raise

    def test_verify_bot_raises_on_id_mismatch(self):
        state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        state.bot_id = 7
        state.save()
        self.srv.enqueue("getMe", {
            "ok": True, "result": {"id": 999, "username": "otherbot", "is_bot": True}
        })
        adapter = self._adapter()
        with self.assertRaises(RuntimeError) as ctx:
            adapter._verify_bot(self._client())
        self.assertIn("mismatch", str(ctx.exception))

    def test_verify_bot_mismatch_error_no_delete_state_advice(self):
        """Error message must not instruct operator to delete the state file."""
        state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        state.bot_id = 7
        state.save()
        self.srv.enqueue("getMe", {
            "ok": True, "result": {"id": 999, "username": "otherbot", "is_bot": True}
        })
        adapter = self._adapter()
        with self.assertRaises(RuntimeError) as ctx:
            adapter._verify_bot(self._client())
        self.assertNotIn("delete", str(ctx.exception).lower())
        self.assertNotIn("Delete", str(ctx.exception))

    def test_webhook_present_raises(self):
        self.srv.enqueue("getWebhookInfo", {
            "ok": True, "result": {"url": "https://example.com/hook", "has_custom_certificate": False}
        })
        adapter = self._adapter()
        with self.assertRaises(RuntimeError) as ctx:
            adapter._check_webhook(self._client())
        self.assertIn("webhook", str(ctx.exception).lower())

    def test_webhook_error_does_not_include_url(self):
        """The webhook URL (which may contain credentials) must not appear in errors."""
        webhook_url = "https://example.com/hook?token=SUPERSECRET"
        self.srv.enqueue("getWebhookInfo", {
            "ok": True, "result": {"url": webhook_url, "has_custom_certificate": False}
        })
        adapter = self._adapter()
        with self.assertRaises(RuntimeError) as ctx:
            adapter._check_webhook(self._client())
        self.assertNotIn(webhook_url, str(ctx.exception))
        self.assertNotIn("SUPERSECRET", str(ctx.exception))

    def test_no_webhook_does_not_raise(self):
        self.srv.enqueue("getWebhookInfo", {
            "ok": True, "result": {"url": "", "has_custom_certificate": False}
        })
        adapter = self._adapter()
        adapter._check_webhook(self._client())   # should not raise

    def test_pairing_nonce_generated_when_unpaired(self):
        adapter = self._adapter()
        self.assertIsNone(adapter.state.pairing_nonce)
        adapter._ensure_pairing_nonce()
        self.assertIsNotNone(adapter.state.pairing_nonce)
        self.assertGreater(len(adapter.state.pairing_nonce), 10)

    def test_pairing_nonce_not_overwritten_on_second_call(self):
        adapter = self._adapter()
        adapter._ensure_pairing_nonce()
        first = adapter.state.pairing_nonce
        adapter._ensure_pairing_nonce()
        self.assertEqual(adapter.state.pairing_nonce, first)

    def test_pairing_nonce_not_generated_when_paired(self):
        state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        state.paired = True
        state.save()
        adapter = self._adapter()
        adapter._ensure_pairing_nonce()
        self.assertIsNone(adapter.state.pairing_nonce)

    # ---- colleague_id / local_thread binding ----

    def test_colleague_id_bound_on_first_run(self):
        """bound_colleague_id is set during _verify_config on first use."""
        adapter = self._adapter(colleague_id="david")
        self.assertIsNone(adapter.state.bound_colleague_id)
        adapter._verify_config("david", None)
        state2 = TelegramChannelState(Path(self.tmp.name) / "state.json")
        self.assertEqual(state2.bound_colleague_id, "david")

    def test_colleague_id_mismatch_raises(self):
        """Changing colleague_id on an established state raises RuntimeError."""
        state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        state.bound_colleague_id = "david"
        state.save()
        adapter = self._adapter(colleague_id="rowan")
        with self.assertRaises(RuntimeError) as ctx:
            adapter._verify_config("rowan", None)
        self.assertIn("mismatch", str(ctx.exception).lower())
        self.assertIn("david", str(ctx.exception))

    def test_colleague_id_match_does_not_raise(self):
        """Same colleague_id as bound: no error."""
        state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        state.bound_colleague_id = "david"
        state.save()
        adapter = self._adapter(colleague_id="david")
        adapter._verify_config("david", None)  # should not raise

    def test_local_thread_mismatch_raises(self):
        """Changing local_thread on an established state raises RuntimeError."""
        state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        state.bound_colleague_id = "david"
        state.bound_local_thread = "codex:old-thread"
        state.save()
        adapter = self._adapter(colleague_id="david", local_thread="codex:new-thread")
        with self.assertRaises(RuntimeError) as ctx:
            adapter._verify_config("david", "codex:new-thread")
        self.assertIn("mismatch", str(ctx.exception).lower())

    def test_local_thread_null_to_non_null_raises_on_established_channel(self):
        """
        Changing null → non-null local_thread on an established channel raises.
        This would silently split history between telegram:<chat> and the new
        thread; explicit migration is future work.
        """
        state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        state.bound_colleague_id = "david"
        state.bound_local_thread = None
        state.save()
        adapter = self._adapter(colleague_id="david", local_thread="codex:new-thread")
        with self.assertRaises(RuntimeError) as ctx:
            adapter._verify_config("david", "codex:new-thread")
        self.assertIn("mismatch", str(ctx.exception).lower())
        # State must be unchanged (bound_local_thread remains None)
        state2 = TelegramChannelState(Path(self.tmp.name) / "state.json")
        self.assertIsNone(state2.bound_local_thread)

    def test_local_thread_removal_raises(self):
        """
        Removing a configured local_thread on an established channel raises.
        config local_thread=None with state bound_local_thread='saved-history'
        would cause UpdateProcessor to use telegram:<chat>, losing the history.
        """
        state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        state.bound_colleague_id = "david"
        state.bound_local_thread = "saved-history"
        state.save()
        adapter = self._adapter(colleague_id="david")  # no local_thread → None
        with self.assertRaises(RuntimeError) as ctx:
            adapter._verify_config("david", None)
        self.assertIn("mismatch", str(ctx.exception).lower())
        self.assertIn("saved-history", str(ctx.exception))
        # State must be unchanged
        state2 = TelegramChannelState(Path(self.tmp.name) / "state.json")
        self.assertEqual(state2.bound_local_thread, "saved-history")

    def test_local_thread_match_does_not_raise(self):
        """Same local_thread as bound: no error."""
        state = TelegramChannelState(Path(self.tmp.name) / "state.json")
        state.bound_colleague_id = "david"
        state.bound_local_thread = "codex:same"
        state.save()
        adapter = self._adapter(colleague_id="david", local_thread="codex:same")
        adapter._verify_config("david", "codex:same")  # should not raise

    def test_local_thread_removal_with_real_store_raises_and_state_unchanged(self):
        """
        Real-Store regression: state.bound_local_thread='saved-history' + config
        local_thread=None. _verify_config must raise so the UpdateProcessor is
        never constructed with the wrong (None) thread, and state is unchanged.
        """
        import tempfile as _tf, json as _json
        with _tf.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            with Store(home_dir) as s:
                s.initialize("Vesper", "test", "testing")

            state_path = Path(self.tmp.name) / "state_rs.json"
            token_path = Path(self.tmp.name) / "token.txt"
            token_path.write_text("12345:Token\n")

            # Establish a paired state with bound_local_thread='saved-history'
            state = TelegramChannelState(state_path)
            state.bot_id = 7
            state.paired = True
            state.bound_user_id = 42
            state.bound_chat_id = 42
            state.bound_colleague_id = "david"
            state.bound_local_thread = "saved-history"
            state.outbox_baseline_at = ""
            state.save()

            cfg_path = Path(self.tmp.name) / "cfg_rs.json"
            # local_thread intentionally absent → None
            cfg_path.write_text(_json.dumps({
                "token_file": str(token_path),
                "colleague_id": "david",
                "employee_home": home_dir,
                "state_file": str(state_path),
                "lock_file": str(Path(self.tmp.name) / "rs.lock"),
            }))
            adapter = TelegramAdapter(str(cfg_path))

            with self.assertRaises(RuntimeError) as ctx:
                adapter._verify_config("david", None)
            self.assertIn("saved-history", str(ctx.exception))

            # State on disk must remain unchanged
            state2 = TelegramChannelState(state_path)
            self.assertEqual(state2.bound_local_thread, "saved-history")


# ---------------------------------------------------------------------------
# Restart and deduplication (uses a real Store)
# ---------------------------------------------------------------------------

class TestRestartAndDeduplication(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.state_path = Path(self.tmp.name) / "state.json"
        with Store(str(self.home)) as s:
            s.initialize("Vesper", "test", "testing")

    def tearDown(self):
        self.tmp.cleanup()

    def _paired_state(self, user_id=42, chat_id=42, bot_id=99):
        state = TelegramChannelState(self.state_path)
        state.bot_id = bot_id
        state.paired = True
        state.bound_user_id = user_id
        state.bound_chat_id = chat_id
        state.save()
        return state

    def test_cursor_persists_across_state_reload(self):
        state = self._paired_state()
        state.update_cursor = 42
        state.save()
        state2 = TelegramChannelState(self.state_path)
        self.assertEqual(state2.update_cursor, 42)

    def test_duplicate_update_id_produces_one_store_event(self):
        """
        The Store's feed() idempotency guard on external_id ensures that
        replaying the same update (same external_id) after a restart does not
        create a duplicate event.
        """
        state = self._paired_state()
        with Store(str(self.home)) as store:
            proc = UpdateProcessor(state, "david")
            upd = _msg_update(100, "first time", user_id=42)
            result1 = proc.process(upd, store)
            # Simulate restart: process same update again (cursor not yet advanced,
            # simulating a crash before cursor save)
            result2 = proc.process(upd, store)

            self.assertTrue(result1["processed"])
            # The second call returns the original event (idempotency)
            self.assertEqual(result1["event_id"], result2["event_id"])

    def test_processor_feeds_different_updates_as_distinct_events(self):
        state = self._paired_state()
        with Store(str(self.home)) as store:
            proc = UpdateProcessor(state, "david")
            upd1 = _msg_update(101, "first", user_id=42)
            upd2 = _msg_update(102, "second", user_id=42)
            r1 = proc.process(upd1, store)
            r2 = proc.process(upd2, store)
            self.assertNotEqual(r1["event_id"], r2["event_id"])

    def test_replay_after_restart_deduplicated_by_cursor_offset(self):
        """
        In normal operation the caller advances the cursor after ingestion.
        A fresh TelegramChannelState loaded from disk has the last cursor,
        so get_updates would use offset=cursor+1, skipping already-processed
        updates at the Telegram API level. This test verifies cursor persistence.
        """
        state = self._paired_state()
        state.update_cursor = 999
        state.save()

        # Reload — simulates adapter restart
        state2 = TelegramChannelState(self.state_path)
        self.assertEqual(state2.update_cursor, 999)
        # The adapter loop would call get_updates(offset=1000), skipping 1..999

    def test_cursor_not_advanced_on_store_error(self):
        """
        When store.feed() raises (store_error), the cursor must NOT advance.
        The update will be retried on the next poll.
        """
        state = self._paired_state()
        # UpdateProcessor returns store_error when feed raises
        class FailStore:
            def feed(self, *a, **kw):
                raise OSError("disk full")
            def identity(self):
                return {"id": "employee-test"}

        proc = UpdateProcessor(state, "david")
        upd = _msg_update(200, "trigger store error", user_id=42)
        result = proc.process(upd, FailStore())

        self.assertEqual(result["reason"], "store_error")
        # Cursor must not have been updated
        state2 = TelegramChannelState(self.state_path)
        self.assertIsNone(state2.update_cursor)

    def test_employee_id_bound_on_first_run(self):
        """_verify_employee binds Store.identity().id on first run."""
        state = self._paired_state()
        self.assertIsNone(state.employee_id)

        # Create a minimal adapter-like setup
        import tempfile as _tf, json as _json
        with _tf.TemporaryDirectory() as td:
            token_path = Path(td) / "token.txt"
            token_path.write_text("12345:Token\n")
            cfg_path = Path(td) / "cfg.json"
            cfg_path.write_text(_json.dumps({
                "token_file": str(token_path),
                "colleague_id": "david",
                "employee_home": str(self.home),
                "state_file": str(self.state_path),
            }))
            adapter = TelegramAdapter(str(cfg_path))

        with Store(str(self.home)) as store:
            adapter._verify_employee(store)
            expected_id = store.identity()["id"]

        state2 = TelegramChannelState(self.state_path)
        self.assertEqual(state2.employee_id, expected_id)

    def test_employee_id_mismatch_raises(self):
        """Moving a state file to a different employee's home must raise RuntimeError."""
        state = self._paired_state()
        state.employee_id = "employee-different-000"
        state.save()

        import tempfile as _tf, json as _json
        with _tf.TemporaryDirectory() as td:
            token_path = Path(td) / "token.txt"
            token_path.write_text("12345:Token\n")
            cfg_path = Path(td) / "cfg.json"
            cfg_path.write_text(_json.dumps({
                "token_file": str(token_path),
                "colleague_id": "david",
                "employee_home": str(self.home),
                "state_file": str(self.state_path),
            }))
            adapter = TelegramAdapter(str(cfg_path))

        with Store(str(self.home)) as store:
            with self.assertRaises(RuntimeError) as ctx:
                adapter._verify_employee(store)
        self.assertIn("mismatch", str(ctx.exception))
        self.assertNotIn("delete", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# Batch cursor hole: batch stops at first store_error
# ---------------------------------------------------------------------------

class TestBatchCursorHole(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = _paired_state(self.tmp.name, user_id=42, chat_id=42, bot_id=99)

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_stops_at_first_store_error(self):
        """
        When the first update in a batch causes store_error, the loop must stop
        immediately. The second update must NOT be processed, and the cursor must
        NOT advance past the failed update.
        """
        feed_calls = []

        class FailFirstStore:
            def __init__(self):
                self._attempts = 0
            def feed(self, sender, body, **kw):
                self._attempts += 1
                feed_calls.append(body)
                if self._attempts == 1:
                    raise OSError("disk full on first call")
                return {"id": f"event-{self._attempts}"}
            def identity(self):
                return {"id": "employee-test"}

        proc = UpdateProcessor(self.state, "david")
        store = FailFirstStore()

        updates = [
            _msg_update(1, "first fails", user_id=42),
            _msg_update(2, "should not reach", user_id=42),
        ]

        # Replicate the loop logic from _run_loop (with the fix applied)
        for update in updates:
            update_id = update["update_id"]
            result = proc.process(update, store)
            if result.get("reason") == "store_error":
                break  # <-- the fix: stop on store_error
            self.state.update_cursor = update_id
            self.state.save()

        # Only the first update was attempted
        self.assertEqual(store._attempts, 1)
        # Cursor never advanced (failed update not acknowledged)
        state2 = TelegramChannelState(Path(self.tmp.name) / "state.json")
        self.assertIsNone(state2.update_cursor)

    def test_batch_continues_after_non_store_error(self):
        """
        Rejected updates (e.g. bot_sender, unrecognized_sender) are not retryable
        store errors; the loop must continue and advance the cursor.
        """
        proc = UpdateProcessor(self.state, "david")
        store = MockStore()

        updates = [
            _msg_update(10, "hello", user_id=999),    # unrecognized_sender → rejected, not store_error
            _msg_update(11, "hello", user_id=42),     # normal message
        ]

        for update in updates:
            update_id = update["update_id"]
            result = proc.process(update, store)
            if result.get("reason") == "store_error":
                break
            self.state.update_cursor = update_id
            self.state.save()

        # Cursor should be at update 11 (both processed, second one succeeded)
        state2 = TelegramChannelState(Path(self.tmp.name) / "state.json")
        self.assertEqual(state2.update_cursor, 11)
        # Only the legitimate message was fed
        self.assertEqual(len(store.fed), 1)


# ---------------------------------------------------------------------------
# _feed_event direct call
# ---------------------------------------------------------------------------

class TestFeedEventHelper(unittest.TestCase):

    def test_passes_in_reply_to_to_supporting_store(self):
        store = MockStore()
        _feed_event(store, sender="david", body="hi", source="test",
                    in_reply_to="123")
        self.assertEqual(store.fed[0]["in_reply_to"], "123")

    def test_passes_none_in_reply_to_when_absent(self):
        store = MockStore()
        _feed_event(store, sender="david", body="hi", source="test")
        self.assertIsNone(store.fed[0]["in_reply_to"])


# ---------------------------------------------------------------------------
# M04 responsiveness — focused unit coverage for the two specific bugs fixed
# ---------------------------------------------------------------------------

class TestAdapterOutboxResponsiveness(unittest.TestCase):
    """
    Unit coverage for the M04 hotfix bugs:
      Bug 1 — inbound poll used POLL_TIMEOUT_SECONDS (30 s), so outbox was only
               checked after a 30-second wait even when the channel was quiet.
      Bug 2 — 429/network backoff called time.sleep(full_wait), so outbox was
               starved for the entire backoff duration instead of being serviced
               at short intervals.
    The functional regressions live in test_telegram_responsiveness.py.
    These unit tests target the two concrete code-level mechanisms.
    """

    def _minimal_paired_adapter(self, root):
        """Return a TelegramAdapter instance with a pre-paired state in *root*."""
        home = Path(root) / "emp"
        with Store(home) as store:
            identity = store.initialize("Aster", "M04 test colleague")
        token_path = Path(root) / "token.txt"
        token_path.write_text("test-token")
        state_path = home / "telegram.json"
        state = TelegramChannelState(state_path)
        state.employee_id = identity["id"]
        state.bot_id = 55
        state.bot_username = "testbot"
        state.paired = True
        state.bound_user_id = 42
        state.bound_chat_id = 42
        state.save()
        cfg_path = Path(root) / "cfg.json"
        cfg_path.write_text(json.dumps({
            "token_file": str(token_path),
            "colleague_id": "david",
            "employee_home": str(home),
            "state_file": str(state_path),
            "lock_file": str(Path(root) / "telegram.lock"),
        }))
        return TelegramAdapter(str(cfg_path))

    def test_poll_timeout_is_outbox_check_interval_not_full_long_poll(self):
        """
        Bug 1: _run_loop must pass OUTBOX_CHECK_INTERVAL (not POLL_TIMEOUT_SECONDS)
        as the timeout to get_updates so the loop returns promptly to service
        pending outbox work when the channel is quiet.
        """
        import unittest.mock
        from synthetics.telegram import OUTBOX_CHECK_INTERVAL, POLL_TIMEOUT_SECONDS

        self.assertLess(OUTBOX_CHECK_INTERVAL, POLL_TIMEOUT_SECONDS,
                        "OUTBOX_CHECK_INTERVAL must be shorter than POLL_TIMEOUT_SECONDS")

        observed_timeout = []

        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._minimal_paired_adapter(tmp)

            class Provider:
                def get_me(self):
                    return dict(id=55, username="testbot", is_bot=True)
                def get_webhook_info(self):
                    return dict(url="")
                def get_updates(self, offset=None, timeout=None):
                    observed_timeout.append(timeout)
                    raise KeyboardInterrupt()  # stop immediately after first call

            with unittest.mock.patch.object(adapter, "_make_client",
                                            return_value=Provider()), \
                    unittest.mock.patch("synthetics.telegram.time.sleep"):
                adapter.run()

        self.assertEqual(len(observed_timeout), 1, "get_updates must have been called once")
        self.assertEqual(
            observed_timeout[0], OUTBOX_CHECK_INTERVAL,
            f"Poll timeout must be OUTBOX_CHECK_INTERVAL ({OUTBOX_CHECK_INTERVAL}), "
            f"got {observed_timeout[0]} — a 30 s long-poll starves outbox when quiet",
        )

    def test_sleep_servicing_outbox_calls_process_outbox_during_intervals(self):
        """
        Bug 2: _sleep_servicing_outbox must call process_outbox after each
        interval sleep so a fresh outbox message is not held up for the full
        backoff duration (429 or network error).
        """
        import unittest.mock
        from synthetics.telegram import OUTBOX_CHECK_INTERVAL

        outbox_calls = []
        clock = [0.0]
        sleeps = []

        def advance(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        class FakeOutbox:
            def process_outbox(self, store):
                outbox_calls.append(1)

        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._minimal_paired_adapter(tmp)

            # Independent review: advance the clock with sleep so this tests the
            # retry deadline without spinning against real elapsed time.
            with unittest.mock.patch("synthetics.telegram.time.sleep", side_effect=advance), \
                    unittest.mock.patch("synthetics.telegram.time.monotonic", side_effect=lambda: clock[0]):
                adapter._sleep_servicing_outbox(
                    float(OUTBOX_CHECK_INTERVAL) * 2.5,
                    FakeOutbox(),
                    object(),  # store is not used by FakeOutbox
                )

        # With a sleep duration of 2.5 intervals the outbox must be serviced at
        # least twice (once after each completed interval).
        self.assertGreaterEqual(
            len(outbox_calls), 2,
            "_sleep_servicing_outbox must call process_outbox after each interval "
            f"({OUTBOX_CHECK_INTERVAL} s); a single long sleep would starve outbox during backoff",
        )
        self.assertGreaterEqual(clock[0], float(OUTBOX_CHECK_INTERVAL) * 2.5)
        self.assertTrue(all(delay <= OUTBOX_CHECK_INTERVAL for delay in sleeps))


if __name__ == "__main__":
    unittest.main()
