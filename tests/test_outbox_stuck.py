"""M27 (ii): senders see their own stuck outbox items; doctor cross-checks channel routes with relay routes.

Origin: question-7ca95827 (11-18 02:05Z) sat pending for 15 minutes because the home's fieldpost channel
record routed `rowan` while the relay's route file had no Vesper->Rowan route; nothing told the sender.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from synthetics import doctor
from synthetics.store import Store

ME = "employee-me"
PEER = "employee-peer"
BOSS = "employee-boss"


def _codes(report, level=None):
    return {f["code"] for f in report["findings"] if level is None or f["level"] == level}


class OutboxStuckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.store = Store(self.home)
        self.store.initialize("Me", "tester")
        self.key = "k" * 40
        self.store.channel_register("fieldpost", Store.credential_hash(self.key), ["fieldpost"], "test",
                                    routes=["boss", "peer"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _later(self, minutes):
        return datetime.now(timezone.utc) + timedelta(minutes=minutes)

    def test_fresh_messages_are_not_stuck(self):
        self.store.message_send("peer", "hi", key="m1")
        stuck = self.store.outbox_stuck()
        self.assertEqual((stuck["count"], stuck["items"], stuck["older_than_48h"]), (0, [], 0))
        self.assertIn("outbox_stuck", self.store.context())

    def test_pending_after_ten_minutes_lists_route_and_question(self):
        self.store.question_ask("peer", "are you there?", key="q1")
        self.store.message_send("operator", "notice", key="m2")
        stuck = self.store.outbox_stuck(now=self._later(11))
        self.assertEqual(stuck["count"], 2)
        by_to = {item["to"]: item for item in stuck["items"]}
        self.assertEqual(by_to["peer"]["routed_by"], ["fieldpost"])
        self.assertEqual(by_to["peer"]["kind"], "question")
        self.assertTrue(by_to["peer"]["question_id"].startswith("question-"))
        self.assertEqual(by_to["peer"]["state"], "pending")
        self.assertGreaterEqual(by_to["peer"]["age_minutes"], 10)
        self.assertEqual(by_to["operator"]["routed_by"], [])  # no channel will ever carry it
        self.assertNotIn("items_all", stuck)

    def test_delivered_messages_drop_out(self):
        message = self.store.message_send("peer", "hi", key="m1")
        self.store.channel_claim(self.key, message["id"], instance="i1")
        self.store.channel_ack(self.key, message["id"], "delivered", receipt="r1", instance="i1")
        self.assertEqual(self.store.outbox_stuck(now=self._later(30))["count"], 0)

    def test_old_leftovers_are_counted_not_listed_and_items_capped(self):
        for n in range(Store.STUCK_ITEMS + 2):
            self.store.message_send("peer", "m%d" % n, key="m%d" % n)
        stuck = self.store.outbox_stuck(now=self._later(20))
        self.assertEqual(stuck["count"], Store.STUCK_ITEMS + 2)
        self.assertEqual(len(stuck["items"]), Store.STUCK_ITEMS)
        old = self.store.outbox_stuck(now=self._later(49 * 60))
        self.assertEqual((old["count"], old["items"], old["older_than_48h"]), (0, [], Store.STUCK_ITEMS + 2))


class RelayCrossCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        store = Store(self.home)
        identity = store.initialize("Me", "tester")
        self.me = identity["id"]
        store.channel_register("fieldpost", Store.credential_hash("k" * 40), ["fieldpost"], "test",
                               routes=["boss", "peer", BOSS, PEER])
        store.close()
        self.routes = Path(self.tmp.name) / "routes.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, recipients, by_home=False):
        rows = []
        for recipient in recipients:
            row = {"recipient_id": recipient, "recipient_home": "/elsewhere/" + recipient}
            row.update({"sender_home": str(self.home)} if by_home else {"sender_id": self.me})
            rows.append(row)
        rows.append({"sender_id": BOSS, "recipient_id": PEER})  # other homes' routes are ignored
        self.routes.write_text(json.dumps({"aliases": {"boss": BOSS, "peer": PEER}, "routes": rows}))

    def _diagnose(self, **kwargs):
        return doctor.diagnose(self.home, relay_routes=self.routes, launchd=lambda label: (False, None), **kwargs)

    def test_missing_relay_route_is_flagged(self):
        # The 11-18 incident: the channel routes the peer, the relay does not carry this home to the peer.
        self._write([BOSS])
        report = self._diagnose()
        self.assertIn("route_without_relay", _codes(report, "warning"))
        self.assertEqual(report["relay"]["route_without_relay"], [PEER, "peer"])
        self.assertTrue(report["ok"])  # a warning, not an error

    def test_matching_routes_are_clean_by_id_or_home(self):
        for by_home in (False, True):
            self._write([BOSS, PEER], by_home=by_home)
            report = self._diagnose()
            self.assertFalse(_codes(report) & {"route_without_relay", "relay_without_channel_route"}, report)

    def test_relay_route_the_channel_lacks_is_flagged(self):
        self._write([BOSS, PEER, "employee-new"])
        report = self._diagnose()
        self.assertIn("relay_without_channel_route", _codes(report, "warning"))
        self.assertEqual(report["relay"]["relay_without_channel_route"], ["employee-new"])

    def test_unregistered_channel_and_bad_file(self):
        self._write([BOSS])
        self.assertIn("relay_channel_unregistered", _codes(self._diagnose(relay_channel="nope"), "info"))
        self.routes.write_text("{not json")
        self.assertIn("relay_routes_invalid", _codes(self._diagnose(), "error"))

    def test_doctor_reports_stuck_and_unroutable(self):
        store = Store(self.home)
        store.message_send("peer", "hi", key="m1")
        store.message_send("operator", "notice", key="m2")
        store.close()
        self._write([BOSS, PEER])
        report = self._diagnose(now=datetime.now(timezone.utc) + timedelta(minutes=15))
        self.assertEqual(report["outbox"]["stuck"], 2)
        self.assertIn("outbox_stuck", _codes(report, "warning"))
        unroutable = [f for f in report["findings"] if f["code"] == "outbox_unroutable"]
        self.assertEqual(len(unroutable), 1)
        self.assertIn("operator", unroutable[0]["detail"])
        self.assertIn("relay", doctor.render(report))




class SendRoutedByTests(unittest.TestCase):
    """Backlog 26: message_send/question_ask never refuse an unroutable `to`; they report routed_by and warn."""

    def test_routed_by_and_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "h")
            store.initialize("T", "t")
            plain = store.message_send("anyone", "hi", key="k0")
            self.assertNotIn("routed_by", plain)  # no channels at all: unchanged (M25 C2)
            store.channel_register("fieldpost", "0" * 64, ["fieldpost"], "operator", routes=["codex-manager"])
            ok = store.message_send("codex-manager", "hi", key="k1")
            self.assertEqual(ok["routed_by"], ["fieldpost"])
            self.assertNotIn("warning", ok)
            bad = store.message_send("operator", "hi", key="k2")
            self.assertEqual(bad["routed_by"], [])
            self.assertIn("No configured channel routes operator", bad["warning"])
            self.assertTrue(any(m["id"] == bad["id"] for m in store.outbox_list()))  # still queued, not refused
            again = store.message_send("operator", "hi", key="k2")  # replay keeps the same id and the warning
            self.assertEqual((again["id"], again["routed_by"]), (bad["id"], []))
            q = store.question_ask("nobody", "?", key="q1")
            self.assertEqual(q["routed_by"], [])
            self.assertNotIn("routed_by", store._get("messages", q["message_id"]))  # not stored
            store.close()


if __name__ == "__main__":
    unittest.main()
