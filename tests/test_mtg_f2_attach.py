"""MTG F2: floor attach runs one meeting (persona, meeting run, lines, cap, wrap event). Fake session; temp homes."""

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from synthetics import floor
from synthetics import floorjournal as fj
from synthetics.store import SESSION_ENV, Store

KEY = b"s" * 32


class FakeSession:
    """Answers each ask at once ("answer: <question>"), except questions containing HOLD (never answered)."""
    instances = []

    def __init__(self, home, run_id, meeting, brief, on_answer, on_error, start_error=None):
        self.run_id, self.meeting, self.brief = run_id, meeting, brief
        self.on_answer, self.on_error, self.start_error = on_answer, on_error, start_error
        self.asks, self.cancelled, self.stopped = [], [], False
        FakeSession.instances.append(self)

    def start(self):
        if self.start_error:
            raise OSError(self.start_error)

    def submit(self, ask):
        self.asks.append(ask)
        if "HOLD" not in ask["question"]:
            self.on_answer(ask["id"], "answer: " + ask["question"])

    def cancel(self, ask_id):
        self.cancelled.append(ask_id)

    def stop(self):
        self.stopped = True
        return 0.25


class AttachMeetingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        with Store(self.home) as store:
            store.initialize("Vesper", "maintainer")
            store.channel_register("stand", Store.credential_hash(KEY), ["stand:"], "test")
            self.work = store.work_put(title="Ship the review", status="ready")["id"]
        (self.home / "floor.json").write_text('{"enabled": true}')  # F4: meetings are opt-in per home
        self.key_file = str(Path(self.temp.name) / "stand.key")
        Path(self.key_file).write_bytes(KEY)
        os.chmod(self.key_file, 0o600)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(SESSION_ENV, None)
        FakeSession.instances = []

    def attach(self, lines, stdin=None, **kw):
        begin = dict(type="begin", meeting="m1", goal="Review status of %s" % self.work, started_by="david")
        begin.update(kw.pop("begin", {}))
        stdin = stdin or io.StringIO("".join(json.dumps(dict(v=1, **line)) + "\n" for line in [begin] + lines))
        stdout = io.StringIO()
        kw.setdefault("session_factory", FakeSession)
        rc = floor.attach(self.home, self.key_file, stdin=stdin, stdout=stdout, **kw)
        return rc, [json.loads(line) for line in stdout.getvalue().splitlines()]

    def test_meeting_round_trip_and_wrap_event(self):
        rc, out = self.attach([dict(type="heard", seq=1, text="hello room"),
                               dict(type="ask", id="a1", rev=1, question="Where is the review?"),
                               dict(type="end")])
        self.assertEqual(rc, 0)
        self.assertEqual([o["type"] for o in out], ["ready", "answer", "ended"])
        ready, answer, ended = out
        self.assertEqual((ready["name"], ready["role"]), ("Vesper", "maintainer"))
        self.assertIn(self.work, FakeSession.instances[0].brief)          # G3: works the goal names
        self.assertEqual((answer["id"], answer["text"], answer["commitments"]),
                         ("a1", "answer: Where is the review?", []))
        self.assertEqual(ended["reason"], "end")
        self.assertTrue(FakeSession.instances[0].stopped)
        with Store(self.home) as store:
            run = store.run_get(ready["run"])
            self.assertEqual((run["kind"], run["status"], run["cost"]), ("meeting", "finished", 0.25))
            self.assertIsNone(store.worker_hold())
            event = store.event_get(ended["wrap_event"])
        self.assertEqual((event["sender"], event["source"]), ("stand", "stand:meeting"))
        body = json.loads(event["body"])
        self.assertEqual((body["meeting"], body["reason"], body["run"]), ("m1", "end", ready["run"]))
        self.assertNotIn("hello room", event["body"])                     # pointer facts only, no heard text
        self.assertEqual(body["head"], fj.head(self.home, "m1"))
        ops = [l["op"] for l in fj.lines(self.home, "m1")]
        self.assertEqual(ops, ["begin", "heard", "ask", "answer", "end"])

    def test_g4_wrap_event_never_answers_a_question(self):
        with Store(self.home) as store:
            q = store.question_ask("david", "Which day?", key="g4-q")["id"]
        self.attach([dict(type="confirm", cid="cid-x", yes=True), dict(type="end")])
        with Store(self.home) as store:
            self.assertEqual(store._find("questions", q)["status"], "open")

    def test_open_ask_at_end_is_journaled_late_not_spoken(self):
        rc, out = self.attach([dict(type="ask", id="a1", rev=0, question="HOLD this"), dict(type="end")])
        self.assertEqual([o["type"] for o in out], ["ready", "ended"])
        late = [l for l in fj.lines(self.home, "m1") if l["op"] == "late"]
        self.assertEqual((late[0]["id"], late[0]["reason"]), ("a1", "meeting end"))

    def test_cap_ends_the_meeting(self):
        read_end, write_end = os.pipe()
        begin = dict(v=1, type="begin", meeting="m1", goal="status", started_by="david")
        os.write(write_end, (json.dumps(begin) + "\n").encode())
        stdin = os.fdopen(read_end, "r")
        try:
            with mock.patch.object(floor, "LINE_POLL", 0.05):
                rc, out = self.attach([], stdin=stdin, cap_seconds=0.3)
        finally:
            os.close(write_end)          # EOF frees the daemon stdin reader before its file is closed
            threading.Event().wait(0.2)
            stdin.close()
        self.assertEqual((rc, out[-1]["type"], out[-1]["reason"]), (0, "ended", "cap"))
        with Store(self.home) as store:
            self.assertIsNone(store.worker_hold())

    def test_session_start_failure_ends_and_releases(self):
        def factory(*args):
            return FakeSession(*args, start_error="no claude")
        rc, out = self.attach([dict(type="end")], session_factory=factory)
        self.assertEqual([o["type"] for o in out], ["error", "ended"])
        self.assertEqual((out[0]["code"], out[1]["reason"]), ("session_failed", "session_failed"))
        with Store(self.home) as store:
            self.assertIsNone(store.worker_hold())
            runs = [r for r in store._all("runs") if r.get("kind") == "meeting"]
        self.assertEqual(runs[0]["status"], "finished")

    def test_bad_lines_are_refused_and_the_meeting_continues(self):
        rc, out = self.attach([dict(type="shout"), dict(type="ask", id="a1", rev=0, question="ok?"),
                               dict(type="ask", id="a1", rev=0, question="again")])
        self.assertEqual([o["type"] for o in out], ["ready", "error", "answer", "error", "ended"])
        self.assertEqual((out[1]["code"], out[3]["code"], out[4]["reason"]), ("bad_line", "conflict", "eof"))

    def test_text_mode_hash_is_on_the_run_and_hides_heard_text(self):
        rc, out = self.attach([dict(type="heard", seq=1, text="secret room words"), dict(type="end")],
                              begin=dict(text_mode="hash"))
        with Store(self.home) as store:
            self.assertEqual(store.run_get(out[0]["run"])["text_mode"], "hash")
        raw = fj.path(self.home, "m1").read_text()
        self.assertNotIn("secret room words", raw)

    def test_g4_doctor_warns_when_a_human_prefix_matches_the_stand_channel(self):
        from synthetics import doctor
        codes = lambda: {f["code"] for f in doctor.diagnose(self.home, launchd=lambda l: (False, None))["findings"]}
        humans = {"version": 1, "default_human": "david", "senders": ["david"], "channel_prefixes": ["telegram:"]}
        (self.home / "humans.json").write_text(json.dumps(humans), encoding="utf-8")
        self.assertNotIn("stand_prefix_as_human", codes())
        humans["channel_prefixes"].append("stand:")
        (self.home / "humans.json").write_text(json.dumps(humans), encoding="utf-8")
        self.assertIn("stand_prefix_as_human", codes())

    def test_bad_begin_fields_refused(self):
        for begin in (dict(goal=""), dict(text_mode="clear")):
            rc, out = self.attach([], begin=begin)
            self.assertEqual((rc, out[0]["code"]), (2, "bad_line"))


if __name__ == "__main__":
    unittest.main()
