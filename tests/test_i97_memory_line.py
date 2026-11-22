"""#97: the meeting session's own prompt says how many active memories the home holds; `ready` and the persona don't.
Temp homes and a stub CLI only; no real claude."""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from synthetics import floorjournal as fj
from synthetics import floorpersona
from synthetics.floorsession import MeetingSession
from synthetics.store import Store

STUB = str(Path(__file__).parent / "fixtures" / "stub_claude_stream.py")
SECRET = "Larkspur"
# Byte for byte the line of the frozen #97 trials (reviews/i97/exp/config-main.json brief_line).
LINE = ("\n\n# Your durable memory\nThis home holds %d active memories, counted when this session process started. "
        "None of them is shown in this brief; memory_search reads them.")


class MemoryLineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "tamsin"
        with Store(self.home) as store:
            self.me = store.initialize("Tamsin", "Release coordinator", purpose="Coordinate releases.")["id"]
        self.argv = Path(self.tmp.name) / "argv.jsonl"
        old = {k: os.environ.get(k) for k in ("STUB_ARGV", "STUB_SLOW")}
        os.environ["STUB_ARGV"], os.environ["STUB_SLOW"] = str(self.argv), "30"
        self.addCleanup(self._restore, old)
        self.answers, self.got = [], threading.Condition()

    @staticmethod
    def _restore(old):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def on_answer(self, ask_id, text):
        with self.got:
            self.answers.append(ask_id)
            self.got.notify_all()

    def add(self, n, start=0):
        with Store(self.home) as store:
            return [store.memory_put(self.me, "note %d" % i, "%s fact %d" % (SECRET, i), source="test")["id"]
                    for i in range(start, start + n)]

    def session(self, brief="brief text"):
        s = MeetingSession(self.home, "run-" + "a" * 32, "m1", brief, self.on_answer, executable=STUB, model=None)
        self.addCleanup(s.stop)
        return s

    @staticmethod
    def prompt(command):
        return command[command.index("--system-prompt") + 1]

    def test_line_is_exact_and_last_for_0_1_and_60(self):
        have = 0
        for n in (0, 1, 60):
            self.add(n - have, have)
            have = n
            prompt = self.prompt(self.session().command())
            self.assertTrue(prompt.endswith("\n# Brief\nbrief text" + LINE % n), prompt[-300:])
            self.assertEqual(prompt.count("# Your durable memory"), 1)

    def test_no_brief_still_has_the_line(self):
        self.assertTrue(self.prompt(self.session(None).command()).endswith("\n# Brief\n" + LINE % 0))

    def test_count_is_active_memories_of_any_subject_like_an_unfiltered_search(self):
        ids = self.add(3)
        with Store(self.home) as store:
            store.entity_put("harbor-sync", "Harbor Sync", "project", "Storage migration project.")
            store.memory_put("harbor-sync", "codename", SECRET, source="test")
            store.memory_put(self.me, "note 0", SECRET + " corrected", source="test", replaces=ids[0])
            self.assertEqual(store.memory_count(), 4)
            self.assertEqual(store.memory_count(), len(store.memory_search(limit=500)))
            self.assertEqual(len(store.memory_search(limit=500, include_superseded=True)), 5)
        self.assertTrue(self.prompt(self.session().command()).endswith(LINE % 4))

    def test_no_memory_text_id_or_subject_in_the_prompt(self):
        ids = self.add(5)
        with Store(self.home) as store:
            store.entity_put("harbor-sync", "Harbor Sync", "project", "Storage migration project.")
            store.memory_put("harbor-sync", "codename", SECRET, source="test")
            brief = floorpersona.build(store, "Team check-in.")["brief"]
        prompt = self.prompt(self.session(brief).command())
        for word in [SECRET, "harbor-sync", "Harbor Sync", "codename", self.me] + ids:
            self.assertNotIn(word, prompt)

    def test_persona_and_ready_are_unchanged_by_memories(self):
        with Store(self.home) as store:
            before = floorpersona.build(store, "Team check-in.")
        self.add(60)
        with Store(self.home) as store:
            after = floorpersona.build(store, "Team check-in.")
        self.assertEqual(json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True))
        self.assertEqual(json.dumps(after["ready"], sort_keys=True), json.dumps(
            {"name": "Tamsin", "role": "Release coordinator", "aliases": [],
             "brief_sha256": after["ready"]["brief_sha256"],
             "persona": "# Who you are\nYou are Tamsin, Release coordinator.\nPurpose: Coordinate releases.\n\n"
                        "# This meeting\nTeam check-in."}, sort_keys=True))
        self.assertNotIn("memor", json.dumps(after).lower())
        self.assertNotIn("memory_count", Path(floorpersona.__file__).read_text(encoding="utf-8"))

    def test_a_cancel_restart_counts_again(self):
        fj.append(self.home, "m1", "begin", {})
        self.add(1)
        s = self.session()
        s.start()
        s.submit(dict(id="a0", rev=0, question="SLOW one"))
        time.sleep(0.5)
        self.add(1, 1)                                  # e.g. an owner's CLI `remember` during the meeting
        self.assertEqual(s.cancel("a0"), "restarted")
        s.submit(dict(id="a1", rev=0, question="two"))
        with self.got:
            self.got.wait_for(lambda: self.answers, timeout=15)
        starts = [json.loads(line)["argv"] for line in self.argv.read_text().splitlines()]
        self.assertEqual([self.prompt(a).endswith(LINE % n) for a, n in zip(starts, (1, 2))], [True, True])
        self.assertEqual(len(starts), 2)

    def test_memory_search_result_is_unchanged(self):
        self.add(2)
        with Store(self.home) as store:
            self.assertEqual(store.memory_search("zzqxnomatch", max_bytes=32000), {"memories": [], "capped": False})


if __name__ == "__main__":
    unittest.main()
