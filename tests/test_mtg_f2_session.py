"""MTG F2: the persistent meeting session (floorsession) against a stub CLI. Temp homes only; no real claude."""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from synthetics import floorjournal as fj
from synthetics import floortools
from synthetics.floorsession import MeetingSession, journal_tail, turn_text
from synthetics.store import Store

STUB = str(Path(__file__).parent / "fixtures" / "stub_claude_stream.py")


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "vesper"
        with Store(self.home) as store:
            store.initialize("Vesper", "maintainer")
        self.argv = Path(self.tmp.name) / "argv.jsonl"
        old = {k: os.environ.get(k) for k in ("STUB_ARGV", "STUB_SLOW")}
        os.environ["STUB_ARGV"] = str(self.argv)
        os.environ["STUB_SLOW"] = "30"

        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        self.answers = []
        self.errors = []
        self.got = threading.Condition()

    def on_answer(self, ask_id, text):
        with self.got:
            self.answers.append((ask_id, text))
            self.got.notify_all()

    def wait_answers(self, n, timeout=15):
        with self.got:
            self.got.wait_for(lambda: len(self.answers) >= n, timeout=timeout)
        return list(self.answers)

    def session(self):
        s = MeetingSession(self.home, "run-" + "a" * 32, "m1", "brief text", self.on_answer, executable=STUB,
                           model=None, on_error=self.errors.append)
        self.addCleanup(s.stop)
        return s

    def ask(self, i, question):
        return dict(id="a%d" % i, rev=0, question=question)

    def test_command_has_no_builtin_tools_and_only_meeting_tools(self):
        command = self.session().command()
        self.assertEqual(command[command.index("--tools") + 1], "")
        allowed = command[command.index("--allowedTools") + 1].split(",")
        self.assertEqual(sorted(allowed), sorted("mcp__synthetics__" + n for n in floortools.MEETING_TOOLS))
        self.assertNotIn("Bash", ",".join(command))
        self.assertEqual(command[command.index("--input-format") + 1], "stream-json")
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertEqual(command.count("--verbose"), 1)
        system = command[command.index("--system-prompt") + 1]
        self.assertIn("live meeting", system)
        self.assertIn("brief text", system)
        mcp = json.loads(command[command.index("--mcp-config") + 1])
        self.assertIn("run-" + "a" * 32, mcp["mcpServers"]["synthetics"]["args"])

    def test_turns_are_serialized_in_order_in_one_process(self):
        s = self.session()
        s.start()
        for i in range(3):
            s.submit(self.ask(i, "question %d" % i))
        answers = self.wait_answers(3)
        self.assertEqual([a for a, _ in answers], ["a0", "a1", "a2"])
        self.assertTrue(answers[0][1].startswith("echo: Meeting ask a0"))
        starts = self.argv.read_text().splitlines()
        self.assertEqual(len(starts), 1)                       # one persistent process, not one per ask
        self.assertEqual(json.loads(starts[0])["run"], "run-" + "a" * 32)
        self.assertAlmostEqual(s.cost(), 0.03, places=4)

    def test_error_result_gives_no_text(self):
        s = self.session()
        s.start()
        s.submit(self.ask(0, "FAIL please"))
        self.assertEqual(self.wait_answers(1), [("a0", None)])

    def test_cancel_queued_drops_it(self):
        s = self.session()
        s.start()
        s.submit(self.ask(0, "SLOW one"))
        s.submit(self.ask(1, "two"))
        self.assertEqual(s.cancel("a1"), "dropped")
        self.assertIsNone(s.cancel("nope"))

    def test_cancel_inflight_restarts_with_journal_tail_and_continues(self):
        fj.append(self.home, "m1", "begin", dict(started_by="david"))
        fj.append(self.home, "m1", "heard", dict(seq=1, text="we ship Friday"))
        s = self.session()
        s.start()
        s.submit(self.ask(0, "SLOW one"))
        s.submit(self.ask(1, "two"))
        time.sleep(0.5)
        self.assertEqual(s.cancel("a0"), "restarted")
        answers = self.wait_answers(1)
        self.assertEqual([a for a, _ in answers], ["a1"])      # a0 never answered, a1 after the restart
        self.assertEqual(len(self.argv.read_text().splitlines()), 2)
        self.assertEqual(s.restarts, 1)

    def test_stop_ends_the_process_and_ignores_late_lines(self):
        s = self.session()
        s.start()
        s.submit(self.ask(0, "SLOW one"))
        time.sleep(0.3)
        process = s.process
        s.stop()
        self.assertIsNotNone(process.poll())
        s.submit(self.ask(1, "after stop"))
        time.sleep(0.3)
        self.assertEqual(self.answers, [])
        self.assertEqual(self.errors, [])                      # a deliberate stop is not an error

    def test_turn_and_tail_text(self):
        self.assertIn("a7", turn_text(dict(id="a7", rev=3, question="Q?")))
        fj.append(self.home, "m2", "begin", {})
        fj.append(self.home, "m2", "ask", dict(id="a1", rev=0, question="when?"))
        self.assertIn("ask a1: when?", journal_tail(self.home, "m2"))


if __name__ == "__main__":
    unittest.main()
