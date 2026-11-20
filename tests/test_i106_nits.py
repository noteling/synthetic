"""#106 follow-up: Rowan's optional nits (issuecomment-7051688186) on the #105 lows.

n1 a directive with content whose release of earlier held messages hits a transient store failure does not feed
the new content (it would jump the messages still held); the redelivered update finishes in arrival order.
n2 Telegram's redelivery of a selection whose release failed does not repeat the ``Talking to:`` notice; a
deliberate second tap (a new update) still confirms.
Same harness as test_i100_p3_chooser (real channel contract into two temporary homes, fake Bot API)."""
import unittest

from synthetics import telegram_homes as th
from tests import test_i100_p2_adapter as p2
from tests import test_i100_p3_chooser as p3

CODEX, QUILL, callback = p3.CODEX, p3.QUILL, p3.callback
TALKING_QUILL = th.ROUTER + "\n" + th.TALKING_TO % QUILL


class NitsTest(unittest.TestCase):
    # The chooser harness without inheriting (and so re-running) its tests.
    setUp, tearDown, events = p3.ChooserTest.setUp, p3.ChooserTest.tearDown, p3.ChooserTest.events
    adapter, texts, bodies = p3.ChooserTest.adapter, p3.ChooserTest.texts, p3.ChooserTest.bodies
    selection, held_with_chooser = p3.ChooserTest.selection, p3.ChooserTest.held_with_chooser

    def flaky(self, a, fail_on):
        """The ``fail_on``-th inbound call (1-based) raises a transient error once."""
        orig = a.contract.call
        count = [0]

        def call(home, name, *args, **kw):
            if name == "inbound":
                count[0] += 1
                if count[0] == fail_on:
                    raise th.ContractError("transport", "simulated transient failure")
            return orig(home, name, *args, **kw)
        a.contract.call = call

    # ---- n1 ----

    def test_n1_directive_content_waits_when_release_fails(self):
        a = self.adapter()
        a.process_update(p2.msg(1, "first held"))
        a.process_update(p2.msg(2, "second held"))
        self.assertEqual([h["update_id"] for h in a.d["held"]], [1, 2])
        self.flaky(a, fail_on=2)                         # "first held" goes through, "second held" fails
        directive = p2.msg(3, "for q: new content")
        self.client.updates = [directive]
        self.assertFalse(a.poll_once())                  # store_error: cursor stays
        self.assertIsNone(a.state.update_cursor)
        self.assertEqual(self.bodies("noteling"), ["first held"])     # the new content did not jump ahead
        self.assertEqual([h["update_id"] for h in a.d["held"]], [2])
        self.client.updates = [directive]                # Telegram redelivers the unconfirmed update
        self.assertTrue(a.poll_once())
        self.assertEqual(a.state.update_cursor, 3)
        self.assertEqual(self.bodies("noteling"), ["first held", "second held", "new content"])
        self.assertEqual(a.d["held"], [])
        self.assertEqual(self.texts().count(TALKING_QUILL), 1)

    # ---- n2 ----

    def test_n2_redelivered_keyboard_tap_confirms_once(self):
        a = self.adapter()
        a.process_update(p2.msg(1, "held"))
        self.flaky(a, fail_on=1)
        tap = p2.msg(2, QUILL)
        self.client.updates = [tap]
        self.assertFalse(a.poll_once())
        self.assertEqual(self.texts().count(TALKING_QUILL), 1)
        self.client.updates = [tap]
        self.assertTrue(a.poll_once())
        self.assertEqual(self.bodies("noteling"), ["held"])
        self.assertEqual(self.texts().count(TALKING_QUILL), 1)

    def test_n2_redelivered_inline_tap_confirms_once(self):
        a = self.adapter()
        data = self.held_with_chooser(a)[1][0]["callback_data"]          # noteling
        self.flaky(a, fail_on=1)
        tap = callback(500, data, qid="tap-1")
        self.client.updates = [tap]
        self.assertFalse(a.poll_once())
        self.assertEqual(self.texts().count(TALKING_QUILL), 1)
        self.client.updates = [tap]
        self.assertTrue(a.poll_once())
        self.assertEqual(self.bodies("noteling"), ["held 1"])
        self.assertEqual(self.texts().count(TALKING_QUILL), 1)

    def test_n2_deliberate_second_tap_still_confirms(self):
        a = self.adapter()
        self.assertEqual(a.process_update(p2.msg(1, QUILL))["reason"], "selected")
        self.assertEqual(a.process_update(p2.msg(2, QUILL))["reason"], "selected")
        self.assertEqual(self.texts().count(TALKING_QUILL), 2)


if __name__ == "__main__":
    unittest.main()
