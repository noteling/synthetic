"""MTG F2: meeting line handling with a fake session (journal, revision/stale, cancel, late, commitments)."""

import tempfile
import unittest
from pathlib import Path

from synthetics import floorjournal as fj
from synthetics.floormeeting import LineError, Meeting


class FakeSession:
    def __init__(self):
        self.turns, self.cancelled = [], []

    def submit(self, turn):
        self.turns.append(turn)

    def cancel(self, ask_id):
        self.cancelled.append(ask_id)


class MeetingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.session = FakeSession()
        self.m = Meeting(self.home, "m1", self.session)
        self.m.begin(started_by="david")

    def ops(self):
        return [(l["op"], l["speaker"]) for l in fj.lines(self.home, "m1")]

    def test_answer_flow_and_speaker_classes(self):
        self.m.handle({"type": "heard", "seq": 1, "text": "When does Byline ship?"})
        self.m.handle({"type": "ask", "id": "a1", "rev": 1, "question": "Byline ship date?"})
        self.assertEqual(self.session.turns, [{"id": "a1", "rev": 1, "question": "Byline ship date?"}])
        out = self.m.finish("a1", "It's in review.")
        self.assertEqual(out, [{"type": "answer", "id": "a1", "rev": 1, "text": "It's in review.", "commitments": []}])
        self.m.handle({"type": "spoken", "seq": 2, "text": "It's in review.", "played": True})
        self.assertEqual(self.ops(), [("begin", fj.FLOOR), ("heard", fj.ROOM), ("ask", fj.FLOOR),
                                      ("answer", fj.FLOOR), ("spoken", fj.PLAYED)])

    def test_panel_correction_after_rev_makes_answer_stale(self):
        self.m.handle({"type": "ask", "id": "a1", "rev": 3, "question": "q"})
        self.m.handle({"type": "note", "seq": 4, "text": "Actually it's Monday"})
        self.assertEqual(self.m.finish("a1", "Friday"), [{"type": "stale", "id": "a1", "reason": "corrected after rev"}])
        self.m.handle({"type": "ask", "id": "a2", "rev": 4, "question": "q"})   # a later ask is not stale
        self.assertEqual(self.m.finish("a2", "Monday")[0]["type"], "answer")

    def test_floor_note_correction_also_stales(self):
        self.m.handle({"type": "ask", "id": "a1", "rev": 1, "question": "q"})
        self.m.floor_note("the date in my notes was wrong")
        self.assertEqual(self.m.finish("a1", "x")[0]["type"], "stale")

    def test_cancel_discards_answer_and_stops_turn(self):
        self.m.handle({"type": "ask", "id": "a1", "rev": 1, "question": "q"})
        self.m.handle({"type": "cancel", "id": "a1"})
        self.assertEqual(self.session.cancelled, ["a1"])
        self.assertEqual(self.m.finish("a1", "too late"), [])
        self.assertEqual([o for o, _ in self.ops()][-2:], ["cancelled", "late"])
        self.assertEqual(self.m.summary()["cancelled"], ["a1"])

    def test_answer_after_end_is_journaled_never_emitted_and_close_anchors_the_head(self):
        self.m.handle({"type": "ask", "id": "a1", "rev": 1, "question": "q"})
        self.m.end()
        with self.assertRaises(LineError):
            self.m.handle({"type": "heard", "seq": 9, "text": "x"})
        self.assertEqual(self.m.finish("a1", "x"), [])            # arrives while the session is being stopped
        head = self.m.close("end")
        self.assertEqual([o for o, _ in self.ops()][-2:], ["late", "end"])
        self.assertEqual(head["lines"], 4)
        self.assertEqual(self.m.finish("a1", "y"), [])            # after close: nothing written
        self.assertEqual(fj.verify(self.home, "m1", head)["chain"], "chain_ok")

    def test_commitments_only_issued_in_that_turn(self):
        self.m.handle({"type": "ask", "id": "a1", "rev": 1, "question": "q"})
        cid = self.m.commit("a1", "I'll send the review notes by Friday")
        self.m.handle({"type": "ask", "id": "a2", "rev": 1, "question": "q2"})
        out1 = self.m.finish("a1", "Yes, by Friday.")
        out2 = self.m.finish("a2", "Sure.")
        self.assertEqual(out1[0]["commitments"], [{"cid": cid, "text": "I'll send the review notes by Friday"}])
        self.assertEqual(out2[0]["commitments"], [])
        with self.assertRaises(LineError):
            self.m.commit("a1", "after the answer")   # no open ask: nothing issued
        self.assertIn(("commit", fj.ISSUED), self.ops())
        self.assertEqual([i["cid"] for i in self.m.summary()["issued"]], [cid])

    def test_commit_from_another_process_shape_is_guarded_by_the_journal(self):
        from synthetics import floormeeting
        self.m.handle({"type": "ask", "id": "a1", "rev": 1, "question": "q"})
        cid = floormeeting.commit(self.home, "m1", "a1", "promise")       # MCP side: no Meeting object
        self.assertEqual(self.m.finish("a1", "ok")[0]["commitments"], [{"cid": cid, "text": "promise"}])
        with self.assertRaises(LineError):
            floormeeting.commit(self.home, "m1", "a1", "after the answer")
        with self.assertRaises(LineError):
            floormeeting.commit(self.home, "m1", "nope", "unknown ask")

    def test_confirm_is_panel_journal_data_only(self):
        self.m.handle({"type": "confirm", "cid": "cid-x", "yes": True})
        self.assertEqual(self.ops()[-1], ("confirm", fj.PANEL))
        self.assertEqual(self.m.summary()["confirmed"], [{"cid": "cid-x", "yes": True, "line": 2}])

    def test_bad_lines(self):
        for line in ({"type": "approve"}, {"type": "heard", "seq": -1, "text": "x"}, {"type": "heard", "seq": 1},
                     {"type": "ask", "id": "a", "rev": True, "question": "q"},
                     {"type": "heard", "seq": 1, "text": "x" * 9000}):
            with self.assertRaises(LineError, msg=line):
                self.m.handle(line)
        self.m.handle({"type": "ask", "id": "a", "rev": 1, "question": "q"})
        with self.assertRaises(LineError):
            self.m.handle({"type": "ask", "id": "a", "rev": 1, "question": "q"})

    def test_hash_mode_summary_has_no_room_words(self):
        m = Meeting(self.home, "m2", self.session, text_mode="hash")
        m.begin()
        m.handle({"type": "heard", "seq": 1, "text": "Zebra42 said this"})
        self.assertNotIn("Zebra42", fj.path(self.home, "m2").read_text(encoding="utf-8"))
        self.assertNotIn("Zebra42", repr(m.summary()))


if __name__ == "__main__":
    unittest.main()
