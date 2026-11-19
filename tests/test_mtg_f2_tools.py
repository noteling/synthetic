"""MTG F2 C5: the meeting tool allow-list and the floor tools (journal-backed). Temp homes only."""

import tempfile
import unittest
from pathlib import Path

from synthetics import floortools, mcp
from synthetics import floorjournal as fj
from synthetics.floormeeting import LineError, Meeting


class Fake:
    def submit(self, turn):
        pass


class ToolsTest(unittest.TestCase):
    def test_allow_list_is_exactly_read_only_five_plus_floor_two(self):
        self.assertEqual(floortools.MEETING_TOOLS, {"memory_search", "work_get", "work_list", "project_memory_search",
                                                    "entities", "floor_commit", "floor_note"})
        names = {t["name"] for t in floortools.filter_tools(mcp.TOOLS + floortools.TOOLS)}
        self.assertEqual(names, floortools.MEETING_TOOLS)
        for banned in ("work_put", "memory_put", "message_send", "question_ask", "question_resolve",
                       "question_close", "routine_rearm", "screen_glance", "runtime_restart", "board_post"):
            self.assertNotIn(banned, names)

    def test_read_only_names_exist_in_mcp(self):
        known = {t["name"] for t in mcp.TOOLS}
        self.assertLessEqual(set(floortools.READ_ONLY), known)
        self.assertFalse(set(floortools.READ_ONLY) & mcp.MUTATIONS)

    def test_floor_tools_write_the_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            m = Meeting(home, "m1", Fake())
            m.begin()
            m.handle({"type": "ask", "id": "a1", "rev": 0, "question": "q"})
            out = floortools.call(home, "m1", "floor_commit", {"ask": "a1", "text": "I'll send it Friday"})
            self.assertTrue(out["cid"].startswith("cid-"))
            floortools.call(home, "m1", "floor_note", {"text": "date corrected"})
            answer = m.finish("a1", "x")[0]   # #76 r1 L4: the session's own floor_note doesn't stale its answer
            self.assertEqual((answer["type"], [c["cid"] for c in answer["commitments"]]), ("answer", [out["cid"]]))
            self.assertEqual([l["speaker"] for l in fj.lines(home, "m1")][-3:], [fj.ISSUED, fj.FLOOR, fj.FLOOR])
            with self.assertRaises(LineError):
                floortools.call(home, "m1", "floor_commit", {"ask": "a1", "text": "late promise"})
            with self.assertRaises(ValueError):
                floortools.call(home, "m1", "work_put", {})



class ServerAllowListTest(unittest.TestCase):
    """C5 through mcp.Server: the meeting run's tools come from the run row, enforced in code."""

    def setUp(self):
        from synthetics.store import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "vesper"
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Vesper", "maintainer")
        self.store.meeting_begin("m1", "stand")
        hold = self.store.meeting_attach("m1")
        self.run = self.store.meeting_run_begin("m1", hold["token"])
        self.bound = Store(self.home, run_id=self.run["id"])
        self.addCleanup(self.bound.close)
        self.server = mcp.Server(self.bound, run_id=self.run["id"])
        self.meeting = Meeting(self.home, "m1", Fake())
        self.meeting.begin()

    def test_tools_list_is_the_allow_list(self):
        listed = self.server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in listed}, floortools.MEETING_TOOLS)

    def test_click_tools_stay_out_of_a_meeting_run_even_when_opted_in(self):
        """Rebase onto #73 (click): a home with click.json enabled still offers a meeting run only MEETING_TOOLS."""
        import json as _json
        from synthetics import click
        (self.home / click.OPT_IN).write_text(_json.dumps({"enabled": True}), encoding="utf-8")
        listed = self.server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in listed}, floortools.MEETING_TOOLS)
        for name in sorted(click.NAMES):
            with self.assertRaisesRegex(ValueError, "Unknown tool", msg=name):
                self.server.call(name, {"request": "click-x"} if name == "screen_click" else {"name": "Save", "why": "x"})

    def test_writes_and_other_tools_refused_floor_tools_work(self):
        for name, args in (("work_put", {"title": "x"}), ("memory_put", {"subject": "x", "predicate": "p",
                                                                          "value": "v", "source": "s"}),
                           ("message_send", {"to": "david", "body": "x", "key": "k"}),
                           ("question_ask", {"to": "david", "body": "x", "key": "k"}),
                           ("employee_context", {})):
            with self.assertRaisesRegex(ValueError, "Unknown tool", msg=name):
                self.server.call(name, args)
        self.assertEqual(self.store.work_list(), [])
        self.meeting.handle({"type": "ask", "id": "a1", "rev": 0, "question": "q"})
        out = self.server.call("floor_commit", {"ask": "a1", "text": "I'll send it Friday"})
        self.assertEqual(out["meeting"], "m1")
        self.assertIsInstance(self.server.call("work_list", {}), list)

    def test_ordinary_run_never_sees_floor_tools(self):
        from synthetics.store import Store
        self.store.meeting_end("m1", token=self.store.worker_hold()["token"])
        self.store.run_finish(self.run["id"], "done")
        run = self.store.run_begin()
        bound = Store(self.home, run_id=run["id"])
        self.addCleanup(bound.close)
        server = mcp.Server(bound, run_id=run["id"])
        names = {t["name"] for t in server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        self.assertFalse(names & floortools.NAMES)
        self.assertIn("work_put", names)
        with self.assertRaisesRegex(ValueError, "Unknown tool"):
            server.call("floor_commit", {"ask": "a1", "text": "x"})


if __name__ == "__main__":
    unittest.main()
