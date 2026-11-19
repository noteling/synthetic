"""Issue #6: during a daily-limit pause an admissible input must be found behind any number of held inputs.

Before the fix the engine and run_begin(event_ids=...) only looked at the 50 oldest pending events, and the runtime
checkpoint reported a pending count capped at 50."""

import sys
import unittest

from synthetics import runtime
from synthetics.engine import Engine
from synthetics.store import Store
from tests import test_runtime_lifecycle as lifecycle
from tests import test_usage as usage

HELD = 60  # more than the 50-event window


class LimitWindowTests(usage.Base):
    def _channels(self, store):
        self.tg_key = b"t" * 32
        store.channel_register("telegram", Store.credential_hash(self.tg_key), ["telegram:"], "test")

    def test_bypass_input_behind_more_than_50_held_inputs_is_admitted(self):
        self.spend(2.0)
        with Store(self.home) as store:
            self._channels(store)
            held = [store.feed("codex-manager", "peer message %d" % n, source="fieldpost")["id"] for n in range(HELD)]
            telegram = store.feed("david", "from Telegram", source="telegram:12345", external_id="tg:1",
                                  credential=self.tg_key)
        brain = usage.Brain()
        engine = Engine(self.home, brain, daily_limit=2.0, limit_notify="david", limit_allow=["david"])
        finished = engine._step()
        self.assertIsNotNone(finished, "the admissible input was never considered")
        self.assertEqual(finished["event_ids"], [telegram["id"]])
        self.assertEqual([e["id"] for e in brain.contexts[0]["events"]], [telegram["id"]])
        with Store(self.home) as store:
            self.assertEqual(store.event_pending_count(), HELD)
            self.assertEqual([e["id"] for e in store.event_pending(limit=100)], held)
        self.assertIsNone(engine._step())  # only held inputs remain

    def test_select_admits_at_most_one_batch_oldest_first(self):
        with Store(self.home) as store:
            ids = [store.feed("david", "m%d" % n)["id"] for n in range(HELD)]
            self.assertEqual([e["id"] for e in store.event_pending_select(lambda e: True)], ids[:50])
            odd = store.event_pending_select(lambda e: e["body"].endswith(("1", "3", "5", "7", "9")), limit=5)
            self.assertEqual([e["id"] for e in odd], [ids[n] for n in (1, 3, 5, 7, 9)])
            self.assertEqual(store.event_pending_select(lambda e: False), [])
            with self.assertRaises(ValueError):
                store.event_pending_select(lambda e: True, limit=0)

    def test_run_begin_captures_exactly_the_named_pending_events_in_seq_order(self):
        with Store(self.home) as store:
            ids = [store.feed("david", "m%d" % n)["id"] for n in range(HELD)]
            run = store.run_begin(event_ids=[ids[-1], "event-unknown", ids[3], ids[-1]])
            self.assertEqual(run["event_ids"], [ids[3], ids[-1]])
            store.run_finish(run["id"], "done")
            # Processed events are never captured again; an empty list still captures nothing.
            again = store.run_begin(event_ids=[ids[-1], ids[3]])
            self.assertEqual(again["event_ids"], [])
            store.run_finish(again["id"], "done")
            self.assertEqual(store.event_pending_count(), HELD - 2)
            plain = store.run_begin()  # unchanged: the ordinary batch is the 50 oldest
            self.assertEqual(plain["event_ids"], [i for i in ids if i not in (ids[3], ids[-1])][:50])


class CheckpointCountTests(lifecycle.Lifecycle.__base__):
    """Runtime restart checkpoint reports the true pending count, not one capped at 50."""

    setUp = lifecycle.Lifecycle.setUp
    controller = lifecycle.Lifecycle.controller
    start = lifecycle.Lifecycle.start
    self_request = lifecycle.Lifecycle.self_request
    idle = lifecycle.Lifecycle.idle
    requests = lifecycle.Lifecycle.requests

    def test_checkpoint_counts_every_pending_event(self):
        first = self.start()
        self.self_request({"timeout": 300})
        with Store(self.home) as store:
            for n in range(HELD):
                store.feed("david", "m%d" % n)
        self.assertTrue(self.idle(first))
        record = self.requests()[0]
        self.assertEqual(record["status"], "restarting")
        self.assertEqual(record["checkpoint"]["pending_events"], HELD)


if __name__ == "__main__":
    unittest.main()
