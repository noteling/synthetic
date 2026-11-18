"""M27 (iv) Board: wake scan (design test 6; C1-C4, Q4)."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from synthetics import board, capture
from synthetics.projects import ProjectError, ProjectStore
from synthetics.store import Store


def post(topic, **kw):
    return [{"kind": "post", "body": dict(topic=topic, body="body of " + topic, **kw)}]


class WakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "vesper")
        self.me = self.store.initialize("Vesper", "maintainer")["id"]
        self.rowan = Store(self.root / "rowan")
        self.rid = self.rowan.initialize("Rowan", "maintainer")["id"]
        with self.ps() as ps:
            ps.grant(self.me, "post", by="operator")
            ps.grant(self.rid, "post", by="operator")
        board.scan(self.store)  # first scan starts at head

    def tearDown(self):
        self.store.close()
        self.rowan.close()
        self.tmp.cleanup()

    def ps(self):
        return ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")

    def add(self, batch, author, **kw):
        with self.ps() as ps:
            return ps.append(batch, author=author, **kw)["ids"][0]

    def test_6_wakes_only_on_mention_reply_owned(self):
        quiet = self.add(post("general"), self.rid)
        mention = self.add(post("ping", mentions=[self.me]), self.rid)
        mine = self.add(post("mine"), self.me)
        reply = self.add(post("re", reply_to=mine), self.rid)
        self.assertEqual(len(board.scan(self.store)), 2)
        evs = self.store.event_pending()
        self.assertEqual([e["external_id"] for e in evs], [mention, reply])
        self.assertTrue(all(e["source"] == "board:synthetics" and e["auth"] is None for e in evs))
        self.assertEqual(evs[1]["thread"], "board:synthetics:" + mine)
        self.assertNotIn(quiet, json.dumps(evs))
        self.assertEqual(board.scan(self.store), [])  # cursor moved; no rewake

    def test_6_owned_work_ref_and_c4_no_work_change(self):
        work = self.store.work_put("fix it", status="waiting", waiting_for="x",
                                   wake_at="2099-01-01T00:00:00Z", publish="synthetics", origin="self")
        board.publish_drain(self.store)
        self.add(post("about it", refs=[work["id"]]), self.rid)
        ids = board.scan(self.store)
        self.assertEqual(len(ids), 1)
        ev = self.store.event_get(ids[0])
        self.assertNotIn("work_ref", ev)
        self.assertEqual(self.store.work_get(work["id"])["status"], "waiting")

    def test_c3_only_first_three_mentions(self):
        self.add(post("many", mentions=["a", "b", "c", self.me]), self.rid)
        self.assertEqual(board.scan(self.store), [])

    def test_c1_thread_cap_per_day(self):
        mine = self.add(post("root"), self.me)
        for i in range(5):
            self.add(post("re%d" % i, reply_to=mine), self.rid)
        day = datetime(2026, 11, 18, 5, tzinfo=timezone.utc)
        self.assertEqual(len(board.scan(self.store, now=day)), board.THREAD_WAKES_PER_DAY)
        self.assertEqual([o["post"] for o in self.store.context()["board_open"]][:1] != [], True)
        self.add(post("next day", reply_to=mine), self.rid)
        self.assertEqual(len(board.scan(self.store, now=datetime(2026, 11, 19, 1, tzinfo=timezone.utc))), 1)

    def test_n3_prune_old_wake_counters_and_orphan_snaps(self):
        mine = self.add(post("root"), self.me)
        self.add(post("re", reply_to=mine), self.rid)
        board.scan(self.store, now=datetime(2026, 11, 18, 5, tzinfo=timezone.utc))
        run = self.store.run_begin()
        board._put_meta(self.store, "board_snap:run-crashed", "{}")
        board._put_meta(self.store, "board_snap:" + run["id"], "{}")
        keys = lambda p: {k for (k,) in self.store.db.execute("SELECT k FROM capture_meta WHERE k LIKE ?", (p + "%",))}
        self.assertTrue(keys("board_wakes:2026-11-18"))
        board.scan(self.store, now=datetime(2026, 11, 18, 23, tzinfo=timezone.utc))  # same day: nothing pruned
        self.assertTrue(keys("board_wakes:2026-11-18"))
        self.assertIn("board_snap:run-crashed", keys("board_snap:"))
        board.scan(self.store, now=datetime(2026, 11, 19, 1, tzinfo=timezone.utc))
        self.assertEqual(keys("board_wakes:"), set())
        self.assertEqual(keys("board_snap:"), {"board_snap:" + run["id"]})  # the running run keeps its heads
        self.assertEqual(board.prune(self.store, "2026-11-19"), {})  # once per day

    def test_n2_board_event_never_bypasses_limit(self):
        cfg = capture.empty_humans()
        cfg["limit_bypass"] = {"channel_prefixes": ["board:"], "senders": [self.rid]}
        ev = {"sender": self.rid, "source": "board:synthetics", "auth": {"channel": "board"}}
        self.assertFalse(capture.may_bypass_limit(ev, cfg, [self.rid]))
        self.assertTrue(capture.may_bypass_limit(dict(ev, source="fieldpost"),
                                                 dict(cfg, limit_bypass={"channel_prefixes": ["fieldpost"],
                                                                         "senders": [self.rid]}), [self.rid]))

    def test_q4_fenced_post_authenticated_unfenced_null(self):
        self.rowan.work_put("w")
        run = self.rowan.run_begin()["id"]
        self.add(post("fenced", mentions=[self.me]), self.rid, run=run, fence=self.rowan.path)
        self.add(post("cli", mentions=[self.me]), self.rid)
        with self.assertRaises(ProjectError):
            self.add(post("forged", mentions=[self.me]), self.rid, run=run)  # run without a fence
        board.scan(self.store)
        auths = [e["auth"] for e in self.store.event_pending()]
        self.assertEqual(auths[0]["channel"], "board")
        self.assertIsNone(auths[1])

    def test_c2_role_other_and_not_uncovered(self):
        (self.store.home / "humans.json").write_text(json.dumps(
            {"version": 1, "senders": [self.rid], "channel_prefixes": ["board"], "secretary_prefixes": []}))
        self.add(post("ping", mentions=[self.me]), self.rid)
        ev = self.store.event_get(board.scan(self.store)[0])
        cfg = capture.humans(self.store)
        self.assertEqual(capture.role_of(ev, "in", cfg), "other")
        self.assertEqual(capture.uncovered(self.store, self.me)["private"], [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM capture_outbox").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
