"""M25 §7 / design test 13: Telegram mirrors claim/ack into the Store delivery state."""
import tempfile
import unittest
from pathlib import Path

from synthetics.store import Store
from synthetics.telegram import OutboxDelivery, TelegramAPIError, TelegramNetworkError
from tests.test_telegram import _paired_state

KEY = b"t" * 40
OTHER = b"o" * 40


class FakeClient:
    def __init__(self, outcomes=None):
        self.sent = []
        self.outcomes = list(outcomes or [])

    def send_message(self, chat_id, text, **kwargs):
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        self.sent.append((chat_id, text))
        return {"message_id": 1000 + len(self.sent)}


class TelegramStoreMirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "home")
        self.store.initialize("Vesper", "maintainer")
        self.store.channel_register("telegram", Store.credential_hash(KEY), ["telegram:"], "test", routes=["david"])
        self.state = _paired_state(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def outbox(self, client, instance="tg-1", credential=KEY):
        return OutboxDelivery(self.state, "david", client, credential=credential, instance=instance)

    def channel_state(self, message_id):
        message = self.store._find("messages", message_id)
        return message["channel_states"][next(iter(message["channel_states"]))]

    def test_accepted_is_acked_delivered_with_receipt(self):
        sent = self.store.message_send("david", "hello", key="a")
        client = FakeClient()
        self.assertEqual(self.outbox(client).process_outbox(self.store), [sent["id"]])
        state = self.channel_state(sent["id"])
        self.assertEqual((state["state"], state["receipt"]), ("delivered", "telegram:42:1001"))
        self.assertEqual(self.store.outbox_list()[0]["delivery"], "delivered")
        # Next pass: nothing resent, no duplicate delivery record.
        self.assertEqual(self.outbox(client).process_outbox(self.store), [])
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(len(self.store._find("messages", sent["id"])["deliveries"]), 1)

    def test_api_rejection_is_failed_and_network_error_is_uncertain(self):
        rejected = self.store.message_send("david", "one", key="f")
        lost = self.store.message_send("david", "two", key="u")
        client = FakeClient([TelegramAPIError("Bad Request", error_code=400), TelegramNetworkError("timeout")])
        self.outbox(client).process_outbox(self.store)
        self.assertEqual(self.channel_state(rejected["id"])["state"], "failed")
        self.assertEqual(self.channel_state(lost["id"])["state"], "uncertain")
        self.assertEqual(client.sent, [])

    def test_restart_turns_old_claim_uncertain_and_never_resends(self):
        sent = self.store.message_send("david", "crash mid-send", key="c")
        self.store.channel_claim(KEY, sent["id"], instance="tg-dead")
        self.state.set_delivery(sent["id"], {"status": "in-flight", "telegram_message_ids": []})
        outbox = self.outbox(FakeClient(), instance="tg-new")
        outbox.recover_inflight()
        self.assertEqual(outbox.process_outbox(self.store), [])
        self.assertEqual(self.store.channel_uncertain(KEY, instance="tg-new")[0]["message_id"], sent["id"])

    def test_unrouted_or_other_channel_claim_blocks_the_send(self):
        self.store.channel_register("desk", Store.credential_hash(OTHER), ["desk:"], "test", routes=["david"])
        sent = self.store.message_send("david", "already out elsewhere", key="d")
        self.store.channel_claim(OTHER, sent["id"], instance="desk-1")
        self.store.channel_ack(OTHER, sent["id"], "delivered", receipt="desk:1", instance="desk-1")
        client = FakeClient()
        self.assertEqual(self.outbox(client).process_outbox(self.store), [])
        self.assertEqual(client.sent, [])
        # A channel without routes delivers nothing (fail closed).
        self.store.channel_register("bare", Store.credential_hash(b"b" * 40), ["bare:"], "test")
        self.store.message_send("david", "needs a route", key="e")
        self.assertEqual(self.outbox(client, credential=b"b" * 40).process_outbox(self.store), [])
        self.assertEqual(client.sent, [])

    def test_without_credential_behaviour_is_unchanged(self):
        self.store.message_send("david", "legacy", key="l")
        client = FakeClient()
        self.assertEqual(len(self.outbox(client, credential=None).process_outbox(self.store)), 1)
        self.assertEqual(len(client.sent), 1)


if __name__ == "__main__":
    unittest.main()
