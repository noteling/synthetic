"""Issue #1 (noteling/synthetic): an identical ack on a LATER claim (e.g. a second retryable failure with receipt
null) was taken as a duplicate of the earlier attempt's ack, leaving the message claimed until the lease expired
and it became uncertain. A repeat of the same ack for the same claim must stay a duplicate."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from synthetics.store import ChannelError, Store

A = b"a" * 32


def later(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class RepeatAckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "home")
        self.store.initialize("Vesper", "maintainer")
        self.store.channel_register("tg", Store.credential_hash(A), ["tg:"], "test", routes=["david"])
        self.mid = self.store.message_send("david", "hello", key="m1")["id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def state(self):
        return self.store._get("messages", self.mid)["channel_states"]["tg"]

    def due_now(self):
        message = self.store._get("messages", self.mid)
        message["channel_states"]["tg"]["retry_after"] = later(-1)
        with self.store._tx():
            self.store._save("messages", message, "test.retry")

    def test_second_identical_retryable_failure_is_recorded(self):
        self.store.channel_claim(A, self.mid, instance="i1")
        self.assertEqual(self.store.channel_ack(A, self.mid, "failed", retryable=True, instance="i1"),
                         ("recorded", "pending"))
        self.due_now()
        self.store.channel_claim(A, self.mid, instance="i1")
        # Same outcome, same receipt (null), new claim: this attempt's outcome, not a duplicate.
        self.assertEqual(self.store.channel_ack(A, self.mid, "failed", retryable=True, instance="i1"),
                         ("recorded", "pending"))
        st = self.state()
        self.assertEqual((st["state"], st["attempt"]), ("pending", 2))
        self.assertNotIn("lease_until", st)
        self.assertEqual(len(self.store._get("messages", self.mid)["deliveries"]), 2)
        # A third attempt can be claimed and delivered normally.
        self.due_now()
        self.store.channel_claim(A, self.mid, instance="i1")
        self.assertEqual(self.store.channel_ack(A, self.mid, "delivered", receipt="tg:9", instance="i1"),
                         ("recorded", "delivered"))

    def test_same_outcome_on_a_later_claim_after_reconcile_is_recorded(self):
        self.store.channel_claim(A, self.mid)
        self.assertEqual(self.store.channel_ack(A, self.mid, "failed", receipt="tg:400"), ("recorded", "failed"))
        self.assertEqual(self.store.channel_reconcile(A, self.mid, "failed", note="retry"), ("recorded", "pending"))
        self.store.channel_claim(A, self.mid)
        self.assertEqual(self.store.channel_ack(A, self.mid, "failed", receipt="tg:400"), ("recorded", "failed"))

    def test_replayed_ack_of_the_same_claim_is_still_duplicate(self):
        self.store.channel_claim(A, self.mid)
        self.assertEqual(self.store.channel_ack(A, self.mid, "failed", retryable=True), ("recorded", "pending"))
        # The adapter retries the same ack line (e.g. after losing the answer): no second attempt is counted.
        self.assertEqual(self.store.channel_ack(A, self.mid, "failed", retryable=True), ("duplicate", "pending"))
        self.assertEqual(self.state()["attempt"], 1)
        self.due_now()
        self.store.channel_claim(A, self.mid)
        self.assertEqual(self.store.channel_ack(A, self.mid, "delivered", receipt="tg:1"), ("recorded", "delivered"))
        self.assertEqual(self.store.channel_ack(A, self.mid, "delivered", receipt="tg:1"), ("duplicate", "delivered"))
        with self.assertRaises(ChannelError) as ctx:
            self.store.channel_ack(A, self.mid, "failed", receipt="tg:400")
        self.assertEqual(ctx.exception.code, "conflict")


if __name__ == "__main__":
    unittest.main()
