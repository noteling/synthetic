"""Docket P1b: relay links, automatic hop notices (Q1), question_hop at the asker (C2, C4), relay_owed."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from synthetics import board, doctor
from synthetics.projects import ProjectError, ProjectStore

from synthetics.mcp import METHODS, MUTATIONS
from synthetics.store import Store


def relayed_body(sender, msg_id, question_id, to):
    """Shape of a Fieldpost-relayed question (fieldpost/relay.py _format_body)."""
    return ("from: %s\nmessage-id: %s\nquestion-id: %s\nto: %s\nwork-id: -\nsent-at: 2026-11-18T10:00:00Z\n\n---\n\n"
            "Can I get a writable checkout?" % (sender, msg_id, question_id, to))


class HopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.mgr = Store(root / "manager")
        self.addCleanup(self.mgr.close)
        self.mgr.initialize("Codex", "manager")
        self.rowan = Store(root / "rowan")
        self.addCleanup(self.rowan.close)
        self.rowan.initialize("Rowan", "maintainer")
        self.mgr_id, self.rowan_id = self.mgr.identity()["id"], self.rowan.identity()["id"]
        self.key = "k" * 64
        self.mgr.channel_register("fieldpost", Store.credential_hash(self.key), ["fieldpost"], "test",
                                  routes=[self.rowan_id])
        # Rowan's side: three works blocked on one checkout question to the manager.
        self.works = [self.rowan.work_put(t) for t in ("R03", "R04", "KT")]
        self.rq = self.rowan.question_ask("codex-manager", "Writable checkout?", key="rq",
                                          work_ids=[w["id"] for w in self.works])
        self.rowan.question_routed(self.rq["id"], self.mgr_id, "fieldpost")
        self.event = self.mgr.feed(self.rowan_id, relayed_body(self.rowan_id, self.rq["message_id"], self.rq["id"],
                                                               "codex-manager"),
                                   source="fieldpost", external_id="m1", credential=self.key)

    def hops(self, store=None):
        return [m for m in (store or self.mgr).outbox_list() if m.get("kind") == "hop"]

    def deliver(self, message):
        """What Fieldpost does with a kind hop row (sender = the manager, recipient home = the asker)."""
        hop = message["hop"]
        return self.rowan.question_hop(hop["question_id"], hop["hop"], hop["since"], hop["via"],
                                       sender=message["sender"], person=hop["person"])

    def ask_david(self, **kw):
        return self.mgr.question_ask("david", "Rowan needs a writable checkout; OK?", key="dq",
                                     relays=[self.rq["id"]], **kw)

    def test_relay_ask_queues_structured_hop(self):
        dq = self.ask_david()
        self.assertEqual(dq["relays"], [dict(question_id=self.rq["id"], asker=self.rowan_id,
                                             event_id=self.event["id"], link_auth=self.event["auth"])])
        [row] = self.hops()
        self.assertEqual(row["to"], self.rowan_id)
        self.assertEqual(row["hop"], dict(question_id=self.rq["id"], hop="with-person", person="david",
                                          since=dq["created_at"], via=dq["id"]))
        self.assertIsNone(row["question_id"])  # never looks like an asked question (docket, outbox_stuck)
        # Idempotent ask: no second notice.
        self.ask_david()
        self.assertEqual(len(self.hops()), 1)

    def test_unknown_relay_refused_and_nothing_queued(self):
        with self.assertRaisesRegex(ValueError, "unknown_relay"):
            self.mgr.question_ask("david", "x", key="bad", relays=["question-" + "0" * 32])
        with self.assertRaisesRegex(ValueError, "unknown_relay"):
            self.mgr.question_ask("david", "x", key="bad2", relays=["question-1234abcd"])
        self.assertEqual(self.mgr.question_list(), [])
        self.assertEqual(self.mgr.outbox_list(), [])

    def test_manual_unauthenticated_event_is_not_a_relay_source(self):
        other = "question-" + "a" * 32
        self.mgr.feed(self.rowan_id, relayed_body(self.rowan_id, "message-x", other, "codex-manager"),
                      source="manual", external_id="m2")
        with self.assertRaisesRegex(ValueError, "unknown_relay"):
            self.mgr.question_ask("david", "x", key="q", relays=[other])

    def test_c2_fieldpost_event_with_null_auth_links_with_null_link_auth(self):
        old = "question-" + "b" * 32
        self.mgr.feed(self.rowan_id, relayed_body(self.rowan_id, "message-y", old, "codex-manager"),
                      source="fieldpost", external_id="m3")  # pre-M22 backfill: auth null
        dq = self.mgr.question_ask("david", "x", key="q", relays=[old])
        self.assertIsNone(dq["relays"][0]["link_auth"])

    def test_answer_queues_answered_pending_relay_and_relay_owed(self):
        dq = self.ask_david()
        self.mgr.feed("david", "Yes, give her one.", reply_to=dq["id"], external_id="d1")
        rows = self.hops()
        self.assertEqual([r["hop"]["hop"] for r in rows], ["with-person", "answered-pending-relay"])
        self.assertIsNone(rows[1]["hop"]["person"])
        owed = self.mgr.relay_owed()
        self.assertEqual([(o["question_id"], o["asker"], o["via"]) for o in owed],
                         [(self.rq["id"], self.rowan_id, dq["id"])])
        self.assertEqual(self.mgr.context()["relay_owed"], owed)
        self.mgr.message_send(self.rowan_id, "David says yes.", key="r", in_reply_to=self.rq["id"])
        self.assertEqual(self.mgr.relay_owed(), [])

    def test_withdraw_hops_back_to_manager(self):
        dq = self.ask_david()
        self.mgr.question_close(dq["id"], "decided myself")
        self.assertEqual([r["hop"]["hop"] for r in self.hops()], ["with-person", "with-manager"])
        self.assertEqual(self.mgr.relay_owed(), [])

    def test_question_relays_adds_link_later(self):
        dq = self.mgr.question_ask("david", "Checkout for Rowan?", key="dq")
        self.mgr.feed("david", "Yes.", reply_to=dq["id"], external_id="d1")
        self.assertEqual(self.hops(), [])
        out = self.mgr.question_relays(dq["id"], [self.rq["id"], self.rq["id"]])
        self.assertEqual([l["question_id"] for l in out["relays"]], [self.rq["id"]])
        [row] = self.hops()
        self.assertEqual(row["hop"]["hop"], "answered-pending-relay")
        self.mgr.question_relays(dq["id"], [self.rq["id"]])  # already linked: no new notice
        self.assertEqual(len(self.hops()), 1)
        self.assertEqual(len(self.mgr.relay_owed()), 1)
        with self.assertRaisesRegex(ValueError, "unknown_relay"):
            self.mgr.question_relays(dq["id"], ["question-" + "c" * 32])

    def test_c4_hop_never_wakes_answers_or_changes_status(self):
        dq = self.ask_david()
        before = [self.rowan.work_get(w["id"]) for w in self.works]
        got = self.deliver(self.hops()[0])
        self.assertEqual((got["hop"], got["hop_person"], got["hop_via"]), ("with-person", "david", dq["id"]))
        self.assertEqual(Store.hop_label(got), "with-david")
        self.mgr.feed("david", "Yes.", reply_to=dq["id"], external_id="d1")
        got = self.deliver(self.hops()[1])
        self.assertEqual(Store.hop_label(got), "answered-pending-relay")
        self.assertEqual(got["status"], "open")
        self.assertIsNone(got["answer_event_id"])
        after = [self.rowan.work_get(w["id"]) for w in self.works]
        for b, a in zip(before, after):
            self.assertEqual(a["status"], "waiting")
            self.assertEqual(a["waiting_question_ids"], b["waiting_question_ids"])
            self.assertNotIn("woken_by_answer", a)
        self.assertEqual(self.rowan.event_pending(), [])  # a hop is not an input event
        self.assertEqual([h["hop"] for h in got["hop_history"]], ["with-person", "answered-pending-relay"])

    def test_hop_idempotent_and_stale_ignored(self):
        self.ask_david()
        row = self.hops()[0]
        self.deliver(row)
        self.assertTrue(self.deliver(row)["already"])
        hop = row["hop"]
        stale = self.rowan.question_hop(hop["question_id"], "with-manager", "2020-01-01T00:00:00Z", "question-x",
                                        sender=self.mgr_id)
        self.assertTrue(stale["stale"])
        self.assertEqual(self.rowan.question_list()[0]["hop"], "with-person")

    def test_c2_forged_hop_rejected_without_state_change(self):
        self.ask_david()
        hop = self.hops()[0]["hop"]
        snapshot = self.rowan.question_list()
        for kwargs, why in ((dict(sender="employee-" + "f" * 32), "not the recipient"),
                            (dict(sender=self.mgr_id, question_id="question-" + "e" * 32), "no question")):
            args = dict(question_id=hop["question_id"], hop="with-person", since=hop["since"], via=hop["via"],
                        person="david")
            args.update(kwargs)
            with self.assertRaisesRegex(ValueError, "hop_rejected.*" + why):
                self.rowan.question_hop(**args)
        self.rowan.feed(self.mgr_id, "Done.", reply_to=self.rq["id"], external_id="ans")
        with self.assertRaisesRegex(ValueError, "hop_rejected.*answered"):
            self.deliver(self.hops()[0])
        self.assertEqual([q.get("hop") for q in self.rowan.question_list()], [None])
        self.assertEqual(len(snapshot), 1)
        with self.assertRaisesRegex(ValueError, "hop_rejected"):
            self.rowan.question_hop(hop["question_id"], "with-david", hop["since"], hop["via"], sender=self.mgr_id)

    def test_hop_is_adapter_only(self):
        self.assertNotIn("question_hop", METHODS)
        self.assertIn("question_relays", MUTATIONS)
        self.assertIn("relays", METHODS["question_ask"]["inputSchema"]["properties"])
        self.ask_david()
        session = Store(self.rowan.home, run_id="run-" + "0" * 32)
        self.addCleanup(session.close)
        with self.assertRaisesRegex(ValueError, "adapter operations"):
            hop = self.hops()[0]["hop"]
            session.question_hop(hop["question_id"], hop["hop"], hop["since"], hop["via"], sender=self.mgr_id,
                                 person="david")

    def test_contract_view_carries_hop(self):
        self.ask_david()
        [view] = self.mgr.channel_pending(self.key)
        self.assertEqual(view["kind"], "hop")
        self.assertEqual(view["hop"]["question_id"], self.rq["id"])

    def test_docket_shows_carries_hop_and_relay_owed(self):
        dq = self.ask_david()
        view = self.mgr.docket(person="david")
        [item] = view["items"]
        self.assertEqual(item["carries"], [dict(question_id=self.rq["id"], asker=self.rowan_id,
                                                link_auth="fieldpost")])
        self.assertIn("carries: %s from %s" % (self.rq["id"], self.rowan_id), view["text"])
        self.mgr.feed("david", "Yes.", reply_to=dq["id"], external_id="d1")
        view = self.mgr.docket(person="david")
        self.assertEqual(view["items"], [])
        self.assertEqual([o["question_id"] for o in view["relay_owed"]], [self.rq["id"]])
        self.assertIn("Relay owed", view["text"])
        # The asker's docket (questions to the manager) shows the reported hop.
        self.deliver(self.hops()[0])
        mine = self.rowan.docket(person=self.mgr_id)
        self.assertEqual(mine["items"][0]["hop"], "with-david")
        self.assertIn("now: with-david", mine["text"])

    def test_doctor_warns_relay_owed_after_4h(self):
        dq = self.ask_david()
        self.mgr.feed("david", "Yes.", reply_to=dq["id"], external_id="d1")
        codes = lambda r: {f["code"] for f in r["findings"]}
        soon = doctor.diagnose(self.mgr.home, launchd=lambda l: (False, None),
                               now=datetime.now(timezone.utc) + timedelta(hours=3))
        self.assertNotIn("relay_owed", codes(soon))
        self.assertEqual(soon["outbox"]["relay_owed"], 1)
        late = doctor.diagnose(self.mgr.home, launchd=lambda l: (False, None),
                               now=datetime.now(timezone.utc) + timedelta(hours=5))
        [finding] = [f for f in late["findings"] if f["code"] == "relay_owed"]
        self.assertEqual(finding["level"], "warning")
        self.assertIn(self.rq["id"], finding["detail"])

    def test_board_blocked_by_is_id_and_hop_only(self):
        path = Path(self.tmp.name) / "projects" / "synthetics" / "memory.sqlite3"
        with ProjectStore(path, "synthetics") as ps:
            ps.grant(self.rowan_id, "post", by="operator")
        for w in self.works:
            self.rowan.work_put(id=w["id"], publish="synthetics", origin="assigned")
        board.publish_drain(self.rowan)
        rows = lambda: [e["body"] for e in ProjectStore(path, "synthetics").entries() if e["kind"] == "backlog"]
        first = rows()
        self.assertEqual({tuple(b["blocked_by"]) for b in first}, {("%s queued" % self.rq["id"],)})
        self.assertNotIn("Writable checkout", str(first))
        self.ask_david()
        self.deliver(self.hops()[0])
        self.assertTrue(board.publish_pending(self.rowan))  # a hop re-projects published works
        board.publish_drain(self.rowan)
        latest = {}
        for b in rows():
            latest[b["work_id"]] = b
        self.assertEqual({tuple(b["blocked_by"]) for b in latest.values()}, {("%s with-david" % self.rq["id"],)})
        with ProjectStore(path, "synthetics") as ps:
            self.assertEqual({tuple(r["blocked_by"]) for r in ps.state()["backlog"].values()},
                             {("%s with-david" % self.rq["id"],)})
            bad = dict(first[0], blocked_by=["%s Writable checkout?" % self.rq["id"]])
            with self.assertRaisesRegex(ProjectError, "blocked_by"):
                ps.append([{"kind": "backlog", "body": bad}], author=self.rowan_id, key="bad")
        # A work without linked questions keeps the v1 field set (and its projection hash).
        plain = self.rowan.work_put("Unblocked")
        self.assertNotIn("blocked_by", board.projection(self.rowan.work_get(plain["id"]), self.rowan))


if __name__ == "__main__":
    unittest.main()
