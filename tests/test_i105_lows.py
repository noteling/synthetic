"""#105 follow-up: Rowan's review lows (rtn/pr105/review.md) on the #100 P3 chooser.

L1 an inline tap whose release hits a transient store failure finishes on Telegram's redelivery (not refused as
a replay); L2 a held Reply to a manager that can't be reached stays held for that manager only; L3 tests for her
surviving mutants X2 (generation bump on release) and X4 (a held Reply released to its own home answers the
question); n1 a directive with content releases earlier held messages first; n2 ``/to@<bot>``.
Same harness as test_i100_p3_chooser (real channel contract into two temporary homes, fake Bot API)."""
import unittest

from synthetics import telegram_homes as th
from tests import test_i100_p2_adapter as p2
from tests import test_i100_p3_chooser as p3

CODEX, QUILL, callback = p3.CODEX, p3.QUILL, p3.callback


class LowsTest(unittest.TestCase):
    # The chooser harness without inheriting (and so re-running) its tests.
    setUp, tearDown, events = p3.ChooserTest.setUp, p3.ChooserTest.tearDown, p3.ChooserTest.events
    adapter, texts, bodies = p3.ChooserTest.adapter, p3.ChooserTest.texts, p3.ChooserTest.bodies
    selection, held_with_chooser = p3.ChooserTest.selection, p3.ChooserTest.held_with_chooser

    def codex_question(self, a):
        c = self.stores["synthetics"].question_ask("david", "codex asks?", key="cq", thread="codex:1")
        qid = c.get("question_id") or c["id"]
        a.process_outbox()
        tg = 1001 + next(i for i, t in enumerate(self.texts()) if t.endswith("codex asks?"))
        return qid, tg

    def q_status(self, qid):
        return self.stores["synthetics"]._find("questions", qid)["status"]

    # ---- L1 ----

    def test_l1_inline_tap_after_store_error_finishes_on_redelivery(self):
        a = self.adapter()
        data = self.held_with_chooser(a)[1][0]["callback_data"]          # noteling
        orig = a.contract.call
        fails = [1]

        def flaky(home, name, *args, **kw):
            if name == "inbound" and fails:
                fails.pop()
                raise th.ContractError("transport", "simulated transient failure")
            return orig(home, name, *args, **kw)
        a.contract.call = flaky
        tap = callback(500, data, qid="tap-1")
        self.client.updates = [tap]
        self.assertFalse(a.poll_once())                 # store_error: cursor stays
        self.assertIsNone(a.state.update_cursor)
        self.assertNotIn("tap-1", a.d.get("callbacks", []))
        self.client.updates = [tap]                     # Telegram redelivers the unconfirmed update
        self.assertTrue(a.poll_once())
        self.assertEqual(a.state.update_cursor, 500)
        self.assertEqual(self.bodies("noteling"), ["held 1"])
        self.assertEqual(a.d.get("held"), [])
        self.assertNotIn("callback_replayed", [r["reason"] for r in a.state._d.get("pairing_rejections", [])])
        self.assertIn("tap-1", a.d["callbacks"])         # recorded once the release finished
        self.assertNotIn(("tap-1", "Out of date. Use the buttons below the message box."), self.client.answers)

    # ---- L2 ----

    def test_l2_keyboard_tap_leaves_held_reply_for_down_home(self):
        a = self.adapter()
        a.process_update(p2.msg(1, CODEX))
        qid, tg = self.codex_question(a)
        a.down["synthetics"] = "test"
        r = a.process_update(p2.msg(2, "answer for codex", reply_to=tg))
        self.assertEqual(r["reason"], "held")
        self.assertEqual(self.texts()[-1], th.ROUTER + "\n" + th.HELD_REPLY_DOWN % "🟦 Codex · Synthetics")
        self.assertNotIn("inline_keyboard", self.client.markups[-1] or {})   # no other manager is offered
        self.assertEqual(a.d["held"][0]["target"], "synthetics")
        r = a.process_update(p2.msg(3, QUILL))          # David switches to Quill for something else
        self.assertEqual((r["reason"], r["released"]), ("selected", 0))
        self.assertEqual(self.events("noteling"), [])
        self.assertEqual(self.q_status(qid), "open")
        self.assertEqual([h["update_id"] for h in a.d["held"]], [2])
        self.assertEqual(self.client.edits, [])
        a.process_update(p2.msg(4, "hi quill"))         # plain messages still go to the selection
        self.assertEqual(self.bodies("noteling"), ["hi quill"])
        # Codex is back (operator fixed it); choosing Codex feeds the reply and answers the question (X4).
        del a.down["synthetics"]
        r = a.process_update(p2.msg(5, CODEX))
        self.assertEqual(r["released"], 1)
        self.assertEqual(self.bodies("synthetics"), ["answer for codex"])
        self.assertEqual(self.q_status(qid), "answered")
        self.assertEqual(a.d["held"], [])
        self.assertEqual(self.client.edits[-1][1], th.ROUTER + "\n" + th.SENT_TO % CODEX)

    def test_l2_inline_tap_releases_untargeted_only_in_order(self):
        a = self.adapter()
        a.process_update(p2.msg(1, CODEX))
        qid, tg = self.codex_question(a)
        a.d["selection"] = None
        a.state.save()
        a.process_update(p2.msg(2, "first"))                                      # no selection -> held
        a.down["synthetics"] = "test"
        a.process_update(p2.msg(3, "answer for codex", reply_to=tg))             # targeted hold
        a.process_update(p2.msg(4, "third"))                                      # no selection -> held
        data = self.client.markups[-1]["inline_keyboard"][1][0]["callback_data"]  # noteling
        self.assertEqual(a.process_update(callback(5, data))["released"], 2)
        self.assertEqual(self.bodies("noteling"), ["first", "third"])
        self.assertEqual([h["update_id"] for h in a.d["held"]], [3])
        self.assertEqual(self.q_status(qid), "open")

    def test_l2_target_no_longer_configured_goes_to_choice(self):
        a = self.adapter()
        a.process_update(p2.msg(1, CODEX))
        _qid, tg = self.codex_question(a)
        a.down["synthetics"] = "test"
        a.process_update(p2.msg(2, "answer for codex", reply_to=tg))
        a.d["held"][0]["target"] = "gone"
        a.state.save()
        self.assertEqual(a.process_update(p2.msg(3, QUILL))["released"], 1)
        self.assertEqual(self.bodies("noteling"), ["answer for codex"])

    def test_l2_plain_message_to_down_home_goes_to_choice_as_its_notice_says(self):
        a = self.adapter()
        a.process_update(p2.msg(1, CODEX))
        a.down["synthetics"] = "test"
        self.assertEqual(a.process_update(p2.msg(2, "plain for codex"))["reason"], "held")
        self.assertEqual(self.texts()[-1], th.ROUTER + "\n" + th.HELD_DOWN_CHOOSE % "🟦 Codex · Synthetics")
        self.assertNotIn("stays held until the operator", th.HELD_DOWN_CHOOSE)
        self.assertIn("inline_keyboard", self.client.markups[-1])
        self.assertEqual(a.process_update(p2.msg(3, QUILL))["released"], 1)
        self.assertEqual(self.bodies("noteling"), ["plain for codex"])

    # ---- L3 ----

    def test_x4_conflict_hold_released_to_replied_home_answers(self):
        a = self.adapter()
        a.process_update(p2.msg(1, QUILL))
        qid, tg = self.codex_question(a)
        a.process_update(p2.msg(2, "for q: hmm", reply_to=tg))                    # conflict -> held
        data = self.client.markups[-1]["inline_keyboard"][0][0]["callback_data"]  # codex
        self.assertEqual(a.process_update(callback(3, data))["reason"], "selected")
        self.assertEqual(self.bodies("synthetics"), ["hmm"])
        self.assertEqual(self.q_status(qid), "answered")

    def test_x2_inline_button_from_before_a_release_is_stale(self):
        a = self.adapter()
        old = self.held_with_chooser(a)[1][0]["callback_data"]                   # noteling, generation 0
        gen = a._gen()
        self.assertEqual(a.process_update(callback(200, old))["reason"], "selected")
        self.assertEqual(a._gen(), gen + 1)
        a.d["selection"] = None
        a.state.save()
        a.process_update(p2.msg(201, "later"))                                    # held under the new generation
        self.assertEqual(a.process_update(callback(202, old))["reason"], "callback_stale")
        self.assertEqual([h["update_id"] for h in a.d["held"]], [201])
        self.assertEqual(self.bodies("noteling"), ["held 1"])

    # ---- nits ----

    def test_n1_directive_with_content_releases_earlier_held_first(self):
        a = self.adapter()
        a.process_update(p2.msg(1, "no selection yet"))
        self.assertEqual(a.process_update(p2.msg(2, "for q: hello quill"))["reason"], "fed")
        self.assertEqual(self.bodies("noteling"), ["no selection yet", "hello quill"])
        self.assertEqual(a.d["held"], [])

    def test_n2_to_at_bot(self):
        a = self.adapter()
        self.assertEqual(a.process_update(p2.msg(1, "/to@testbot"))["reason"], "command")
        self.assertEqual(a.process_update(p2.msg(2, "/to@testbot q"))["reason"], "selected")
        self.assertEqual(self.selection(a), "noteling")
        self.assertEqual(a.process_update(p2.msg(3, "/tooth"))["reason"], "fed")   # not a command
        self.assertEqual(self.bodies("noteling"), ["/tooth"])


if __name__ == "__main__":
    unittest.main()
