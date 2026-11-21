"""#113: outbox_drop turns every channel state that is not final (pending, claimed, uncertain) into failed, and leaves
final states (delivered, failed) as they were. delivery_summary already reads a dropped message as failed, so only the
per-channel states show a missed one. Synthetic fixtures only."""

import secrets
import unittest

from synthetics.store import ChannelError, Store

from tests.test_satchel_p2 import Fixture


class DropEveryNonFinalState(Fixture):
    def setUp(self):
        super().setUp()
        store = Store(self.home)
        self.keys = {"desk": self.key}
        for name in ("b", "c", "d", "e"):
            self.keys[name] = secrets.token_hex(32)
            store.channel_register(name, Store.credential_hash(self.keys[name]), [name + ":"], "operator",
                                   routes=["colleague"])
        # mixed: claimed on desk, pending (retryable failure) on b, uncertain on c, failed (final) on d
        self.mixed = store.message_send("colleague", "mixed hello", key="m4")
        store.channel_claim(self.keys["desk"], self.mixed["id"])
        store.channel_claim(self.keys["b"], self.mixed["id"])
        store.channel_ack(self.keys["b"], self.mixed["id"], "failed", retryable=True)
        store.channel_claim(self.keys["c"], self.mixed["id"])
        store.channel_ack(self.keys["c"], self.mixed["id"], "uncertain")
        store.channel_claim(self.keys["d"], self.mixed["id"])
        store.channel_ack(self.keys["d"], self.mixed["id"], "failed", receipt="d-final")
        # partly delivered: claimed on desk, then delivered on e
        self.partly = store.message_send("colleague", "partly hello", key="m5")
        store.channel_claim(self.keys["desk"], self.partly["id"])
        store.channel_claim(self.keys["e"], self.partly["id"])
        store.channel_ack(self.keys["e"], self.partly["id"], "delivered", receipt="e-ok")
        store.close()
        archive = self.extract()["archive"]
        held = self.recreate(archive, source_lost=True, attest="lost")["held_messages"]
        self.assertIn(self.mixed["id"], held)
        self.assertIn(self.partly["id"], held)
        self.store = Store(self.root / "new")
        self.addCleanup(self.store.close)

    def states(self, message_id):
        message = self.store._find("messages", message_id)
        return {name: dict(state) for name, state in message["channel_states"].items()}

    def test_precondition_restored_states_are_mixed(self):
        self.assertEqual({k: v["state"] for k, v in self.states(self.mixed["id"]).items()},
                         {"desk": "claimed", "b": "pending", "c": "uncertain", "d": "failed"})

    def test_drop_fails_pending_claimed_and_uncertain(self):
        self.store.outbox_drop(self.mixed["id"], by="david", reason="sent before loss")
        states = self.states(self.mixed["id"])
        for name in ("desk", "b", "c"):
            self.assertEqual(states[name]["state"], "failed", name)
            self.assertEqual(states[name]["reason"], "dropped after restore: sent before loss", name)
            for key in ("lease_until", "claimed_at", "retry_after"):
                self.assertNotIn(key, states[name], (name, key))
        self.assertEqual(states["d"]["state"], "failed")
        self.assertEqual(states["d"]["receipt"], "d-final")
        self.assertNotIn("reason", states["d"])  # a final state keeps its own record

    def test_drop_keeps_delivered_and_fails_the_claim(self):
        self.store.outbox_drop(self.partly["id"], by="david", reason="already seen")
        states = self.states(self.partly["id"])
        self.assertEqual(states["e"]["state"], "delivered")
        self.assertEqual(states["e"]["receipt"], "e-ok")
        self.assertEqual(states["desk"]["state"], "failed")
        self.assertEqual(self.store.delivery_summary(self.store._find("messages", self.partly["id"])), "delivered")

    def test_no_channel_can_act_on_a_dropped_message(self):
        self.store.outbox_drop(self.mixed["id"], by="david", reason="sent before loss")
        for name in ("desk", "b", "c", "d"):
            self.store.channel_register(name, Store.credential_hash(self.keys[name]), [name + ":"], "operator",
                                        routes=["colleague"])
            self.assertNotIn(self.mixed["id"],
                             [m["message_id"] for m in self.store.channel_pending(self.keys[name])], name)
            with self.assertRaises(ChannelError, msg=name):
                self.store.channel_ack(self.keys[name], self.mixed["id"], "delivered", receipt="late")


if __name__ == "__main__":
    unittest.main()
