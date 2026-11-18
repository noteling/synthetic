"""M25 §2-3: per message x channel delivery state, routing isolation, no blind resend (design tests 3-7)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from synthetics.store import ChannelError, Store

A, B, C = b"a" * 32, b"b" * 32, b"c" * 32


def later(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class DeliveryStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "home")
        self.store.initialize("Vesper", "maintainer")
        self.store.channel_register("tg", Store.credential_hash(A), ["tg:"], "test", routes=["david"])
        self.store.channel_register("desk", Store.credential_hash(B), ["desk:"], "test", routes=["david"])
        self.store.channel_register("peers", Store.credential_hash(C), ["fieldpost"], "test", routes=["codex-manager"])
        self.to_david = self.store.message_send("david", "hello", key="m1")
        self.to_peer = self.store.message_send("codex-manager", "status", key="m2")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def code(self, fn, *args, **kwargs):
        with self.assertRaises(ChannelError) as ctx:
            fn(*args, **kwargs)
        return ctx.exception.code

    def ids(self, rows):
        return [r["message_id"] for r in rows]

    # test 3
    def test_happy_path_idempotent_and_conflict(self):
        mid = self.to_david["id"]
        self.assertEqual(self.ids(self.store.channel_pending(A)), [mid])
        claimed = self.store.channel_claim(A, mid, instance="i1")
        self.assertEqual((claimed["state"], claimed["v"], claimed["to"]), ("claimed", 1, "david"))
        self.assertEqual(self.code(self.store.channel_claim, A, mid, instance="i1"), "conflict")
        self.assertEqual(self.store.channel_ack(A, mid, "delivered", receipt="tg:1:2", instance="i1"),
                         ("recorded", "delivered"))
        self.assertEqual(self.store.channel_ack(A, mid, "delivered", receipt="tg:1:2", instance="i1"),
                         ("duplicate", "delivered"))
        self.assertEqual(self.code(self.store.channel_ack, A, mid, "failed", instance="i1"), "conflict")
        self.assertEqual(self.store.outbox_list(status="delivered")[0]["id"], mid)
        self.assertEqual(self.code(self.store.channel_ack, A, self.to_david["id"] + "x", "delivered"),
                         "unknown_message")

    def test_ack_without_claim_is_invalid(self):
        self.assertEqual(self.code(self.store.channel_ack, A, self.to_david["id"], "delivered"), "invalid_state")

    # test 4
    def test_isolation(self):
        peer = self.to_peer["id"]
        self.assertEqual(self.ids(self.store.channel_pending(A)), [self.to_david["id"]])
        self.assertEqual(self.ids(self.store.channel_pending(C)), [peer])
        for call in (lambda: self.store.channel_claim(A, peer),
                     lambda: self.store.channel_ack(A, peer, "delivered"),
                     lambda: self.store.channel_reconcile(A, peer, "delivered"),
                     lambda: self.store.delivery_record(peer, A, receipt="x")):
            with self.assertRaises(ChannelError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "not_routed")
        self.assertEqual(self.code(self.store.channel_pending, b"z" * 32), "auth_failed")
        # A channel registered without routes delivers nothing (fail closed).
        self.store.channel_register("bare", Store.credential_hash(b"n" * 32), ["bare:"], "test")
        self.assertEqual(self.store.channel_pending(b"n" * 32), [])
        self.assertEqual(self.code(self.store.delivery_record, peer, b"n" * 32), "not_routed")
        # Rotation without --route keeps routes.
        self.store.channel_register("peers", Store.credential_hash(b"r" * 32), ["fieldpost"], "test")
        self.assertEqual(self.ids(self.store.channel_pending(b"r" * 32)), [peer])

    def test_event_status_isolated(self):
        event = self.store.feed("david", "hi", source="tg:1", external_id="x1", credential=A)
        self.assertEqual(self.store.channel_event_status(A, event["id"])["state"], "accepted")
        self.assertEqual(self.code(self.store.channel_event_status, B, event["id"]), "not_routed")

    # test 5
    def test_expired_claim_becomes_uncertain_never_pending(self):
        mid = self.to_david["id"]
        self.store.channel_claim(A, mid, instance="i1")
        message = self.store._get("messages", mid)
        message["channel_states"]["tg"]["lease_until"] = later(-1)
        with self.store._tx():
            self.store._save("messages", message, "test.expire")
        self.assertEqual(self.store.channel_pending(A, instance="i1"), [])
        self.assertEqual([r["state"] for r in self.store.channel_uncertain(A)], ["uncertain"])
        self.assertEqual(self.store.outbox_list(status="uncertain")[0]["id"], mid)
        self.assertEqual(self.code(self.store.channel_ack, A, mid, "delivered", instance="i1"), "invalid_state")
        self.assertEqual(self.store.channel_reconcile(A, mid, "failed", note="no evidence"), ("recorded", "pending"))
        self.assertEqual(self.store.channel_reconcile(A, mid, "failed", note="no evidence"), ("duplicate", "pending"))
        rows = self.store.channel_pending(A)
        self.assertEqual((self.ids(rows), rows[0]["attempt"]), ([mid], 1))
        self.store.channel_claim(A, mid)
        self.store.channel_ack(A, mid, "uncertain")
        self.assertEqual(self.store.channel_reconcile(A, mid, "delivered", receipt="tg:9"), ("recorded", "delivered"))

    def test_new_instance_turns_old_claims_uncertain(self):
        mid = self.to_david["id"]
        self.store.channel_claim(A, mid, instance="i1")
        self.assertEqual(self.store.channel_pending(A, instance="i2"), [])
        uncertain = self.store.channel_uncertain(A)
        self.assertEqual((uncertain[0]["claim_instance"], "replaced" in uncertain[0]["reason"]), ("i1", True))

    def test_reconcile_requires_uncertain_or_failed(self):
        self.assertEqual(self.code(self.store.channel_reconcile, A, self.to_david["id"], "delivered"), "invalid_state")

    # test 6
    def test_retryable_failure_returns_after_retry_after_permanent_stays(self):
        mid = self.to_david["id"]
        self.store.channel_claim(A, mid)
        self.assertEqual(self.store.channel_ack(A, mid, "failed", retryable=True, detail={"http": 429}),
                         ("recorded", "pending"))
        self.assertEqual(self.store.channel_pending(A), [])
        self.assertEqual(self.code(self.store.channel_claim, A, mid), "invalid_state")
        message = self.store._get("messages", mid)
        message["channel_states"]["tg"]["retry_after"] = later(-1)
        with self.store._tx():
            self.store._save("messages", message, "test.retry")
        self.assertEqual(self.store.channel_pending(A)[0]["attempt"], 1)
        self.store.channel_claim(A, mid)
        self.assertEqual(self.store.channel_ack(A, mid, "failed", receipt="tg:400"), ("recorded", "failed"))
        self.assertEqual(self.store.channel_pending(A), [])
        self.assertEqual(self.store.channel_reconcile(A, mid, "failed", note="operator retry"), ("recorded", "pending"))

    # test 7
    def test_delivered_by_one_channel_leaves_every_pending(self):
        mid = self.to_david["id"]
        self.store.channel_claim(B, mid)
        self.store.channel_claim(A, mid)
        self.store.channel_ack(A, mid, "delivered", receipt="tg:1")
        self.assertEqual(self.store.channel_pending(B), [])
        # Q2: a late ack from the other channel is recorded, not a conflict.
        self.assertEqual(self.store.channel_ack(B, mid, "delivered", receipt="desk:1"), ("recorded", "delivered"))
        self.assertEqual(self.store.outbox_list()[0]["delivery"], "delivered")

    def test_refused_in_session(self):
        run = self.store.run_begin([])
        session = Store(self.store.home, run_id=run["id"])
        try:
            self.assertEqual(self.code(session.channel_pending, A), "in_session")
        finally:
            session.close()


class OutboxListMCPTests(unittest.TestCase):
    """Backlog 6: MCP outbox_list is bounded by default and filters on the delivery summary."""

    def test_default_limit_and_status_filter(self):
        from synthetics.mcp import Server
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "home")
            store.initialize("Vesper", "maintainer")
            store.channel_register("tg", Store.credential_hash(A), ["tg:"], "test", routes=["david"])
            for n in range(60):
                store.message_send("david", "m%d" % n, key="k%d" % n)
            first = store.outbox_list()[0]
            store.channel_claim(A, first["id"])
            store.channel_ack(A, first["id"], "delivered", receipt="tg:1")
            server = Server(store)

            def call(args):
                return server.call("outbox_list", args)
            self.assertEqual(len(call({})), 50)
            self.assertEqual(call({})[-1]["body"], "m59")
            self.assertEqual([m["id"] for m in call({"status": "delivered"})], [first["id"]])
            self.assertEqual(len(call({"status": "pending", "limit": 5})), 5)
            store.close()


if __name__ == "__main__":
    unittest.main()
