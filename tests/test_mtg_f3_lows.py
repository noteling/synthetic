"""MTG F3-lows (Rowan's #79 review, Codex ruling 21:06Z 11-19): L1 the verify exemption needs a question created at
or after the meeting's begin; L2 only the stand channel's wrap counts, an impostor wrap is ignored. Fail-first on
the F3 tree (f48f8b4)."""

import json
import unittest

from synthetics import floor
from synthetics import floorjournal as fj
from synthetics import channel as channel_mod
from synthetics.store import Store
from tests import test_mtg_f3_gate as f3  # module import: never re-collect the F3 test classes here

flooraccept = f3.flooraccept
TG_KEY = b"t" * 32


def begin_at(home, meeting):
    return [l for l in fj.lines(home, meeting) if l["op"] == "begin"][0]["at"]


class L1QuestionAfterBegin(f3.Base):
    def test_old_answered_question_no_longer_exempts(self):
        self.promise_meeting()
        q_old = f3.G2Verify._question(self, "answered", "david", {"channel": "fieldpost"},
                                    created_at="2026-01-01T00:00:00Z")
        q_new = f3.G2Verify._question(self, "answered", "david", {"channel": "fieldpost"})
        run, _ = self.run_with_calls([
            ("work_put", {"title": "Old from m1", "evidence": [{"level": "attempted", "ref": q_old}]}),
            ("work_put", {"title": "New from m1", "evidence": [{"level": "attempted", "ref": q_new}]}),
        ])
        result = flooraccept.verify(self.home, run)
        self.assertEqual([v["receipt_line"] for v in result["violations"]], [1], result)

    def test_compared_as_instants_not_text(self):
        self.promise_meeting()
        began = begin_at(self.home, "m1")              # e.g. 2026-11-19T21:30:05.123456Z
        if began[-8:-1] == ".000000":
            self.skipTest("begin landed on a whole second")
        whole = began[:began.index(".")] + "Z"         # the same second, before begin; sorts after it as text
        q = f3.G2Verify._question(self, "answered", "david", {"channel": "fieldpost"}, created_at=whole)
        run, _ = self.run_with_calls([("work_put", {"title": "From m1", "evidence": [{"level": "attempted",
                                                                                   "ref": q}]})])
        self.assertEqual(flooraccept.verify(self.home, run)["verdict"], "violations")

    def run_with_calls(self, calls):
        return f3.G2Verify.run_with(self, calls)


class L2StandWrapOnly(f3.Base):
    def setUp(self):
        super().setUp()
        with Store(self.home) as store:
            store.channel_register("tg", Store.credential_hash(TG_KEY), ["tg:"], "test")

    def plant(self, meeting):
        """Another registered channel writes floor:<meeting>:wrap first (Rowan P3/P4)."""
        with Store(self.home) as store:
            channel = store._channel_by_key(TG_KEY)
            body = json.dumps(dict(meeting=meeting, head=dict(line=1, sha256="0" * 64)), sort_keys=True)
            return channel_mod._inbound_one(store, channel, TG_KEY, dict(
                external_id="floor:%s:wrap" % meeting, sender="tg", body=body, source="tg:meeting"))

    def test_wrap_channel_is_the_floor_channel(self):
        self.assertEqual(flooraccept.WRAP_CHANNEL, floor.CHANNEL)

    def test_impostor_alone_is_not_a_wrap(self):
        planted = self.plant("m4")
        fj.append(self.home, "m4", "begin", {})
        fj.append(self.home, "m4", "end", dict(reason="end"))
        with Store(self.home) as store:
            rows = flooraccept.wrap_rows(store.db, "m4")
            self.assertTrue(rows, planted)
            self.assertIsNone(flooraccept.wrap_event(rows, "m4"))

    def test_impostor_before_the_real_wrap_is_ignored_by_accept(self):
        self.plant("m2")
        cid, wrap = self.promise_meeting(meeting="m2")
        run = self.wrap_run()
        with Store(self.home, run_id=run) as store:
            work = store.floor_accept("m2", cid, "Send review notes")
        self.assertEqual(work["origin"], "meeting")
        self.assertEqual(work["evidence"][0]["ref"], wrap)

    def test_impostor_is_not_a_wrap_for_verify(self):
        self.plant("m1")
        cid, wrap = self.promise_meeting()
        run = self.wrap_run()
        server = self.server(run)
        server.call("floor_accept", {"meeting": "m1", "cid": cid, "title": "Send review notes"})
        with Store(self.home) as store:
            store.run_finish(run, "wrap")
        result = flooraccept.verify(self.home, run)
        self.assertEqual([m["wrap_event"] for m in result["meetings"]], [wrap], result)
        self.assertEqual(result["verdict"], "clean", result)


if __name__ == "__main__":
    unittest.main()
