"""MTG F3: the gate. floor_accept (G1, Codex K1/K2), floor verify (G2, Q2/K3), origin meeting (Q1), the busy-path
nit (G4/K4). Fake meeting session, temp homes; fail-first on ea056f5."""

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from synthetics import floor, floormeeting
from synthetics import floorjournal as fj
from synthetics import mcp
from synthetics.store import SESSION_ENV, Store, timestamp
from tests import test_authority as _warrant  # module import: a bare WarrantTest name would be collected here

try:
    from synthetics import flooraccept
except ImportError:  # fail-first on a base without F3
    flooraccept = None

KEY = b"s" * 32
PROMISE = "I'll send the review notes by Friday"


class PromiseSession:
    """Answers each ask at once. A question containing PROMISE first issues a floor_commit (with WORK=<id> naming
    an existing work), as the real session does through its floor_commit tool."""

    def __init__(self, home, run_id, meeting, brief, on_answer, on_error):
        self.home, self.meeting, self.on_answer = home, meeting, on_answer

    def start(self):
        pass

    def submit(self, ask):
        if "PROMISE" in ask["question"]:
            work = ask["question"].split("WORK=", 1)[1].split()[0] if "WORK=" in ask["question"] else None
            floormeeting.commit(self.home, self.meeting, ask["id"], PROMISE, work_id=work, due="Friday")
        self.on_answer(ask["id"], "answer: " + ask["question"])

    def cancel(self, ask_id):
        pass

    def stop(self):
        return 0.1


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "vesper"
        with Store(self.home) as store:
            store.initialize("Vesper", "maintainer")
            store.channel_register("stand", Store.credential_hash(KEY), ["stand:"], "test")
        self.key_file = str(self.root / "stand.key")
        Path(self.key_file).write_bytes(KEY)
        os.chmod(self.key_file, 0o600)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(SESSION_ENV, None)

    def meeting(self, lines, meeting="m1"):
        begin = dict(type="begin", meeting=meeting, goal="Weekly review", started_by="david")
        stdin = io.StringIO("".join(json.dumps(dict(v=1, **line)) + "\n" for line in [begin] + lines))
        stdout = io.StringIO()
        rc = floor.attach(self.home, self.key_file, stdin=stdin, stdout=stdout, session_factory=PromiseSession)
        out = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(rc, 0, out)
        return out

    def promise_meeting(self, extra=(), question="PROMISE notes?", meeting="m1"):
        out = self.meeting([dict(type="heard", seq=1, text="the deadline moved to Monday"),
                            dict(type="ask", id="a1", rev=1, question=question)] + list(extra) + [dict(type="end")],
                           meeting=meeting)
        answer = [o for o in out if o["type"] == "answer"][0]
        return answer["commitments"][0]["cid"], out[-1]["wrap_event"]

    def wrap_run(self):
        """The ordinary session that receives the wrap event (it captures the pending inputs)."""
        with Store(self.home) as store:
            return store.run_begin()["id"]

    def server(self, run_id):
        store = Store(self.home, run_id=run_id)
        self.addCleanup(store.close)
        return mcp.Server(store, run_id=run_id)

    def audits(self, prefix="floor."):
        with Store(self.home) as store:
            rows = store.db.execute("SELECT action, record_id, data FROM audit ORDER BY seq").fetchall()
        return [(r["action"], r["record_id"], json.loads(r["data"])) for r in rows if r["action"].startswith(prefix)]

    def meeting_works(self):
        with Store(self.home) as store:
            return [w for w in store.work_list() if w.get("origin") == "meeting"]


class G1Accept(Base):
    def test_accept_creates_one_meeting_work_verbatim_with_wrap_evidence(self):
        cid, wrap = self.promise_meeting()
        run = self.wrap_run()
        with Store(self.home, run_id=run) as store:
            work = store.floor_accept("m1", cid, "Send review notes", priority=2)
        self.assertEqual((work["origin"], work["title"], work["priority"], work["status"]),
                         ("meeting", "Send review notes", 2, "ready"))
        self.assertEqual(work["description"], PROMISE + "\n\nDue (as said): Friday")
        self.assertEqual(work["meeting"]["cid"], cid)
        self.assertEqual(work["meeting"]["wrap_event"], wrap)
        self.assertEqual([(e["ref"], e["level"]) for e in work["evidence"]], [(wrap, "observed")])
        self.assertIn("journal line %d" % work["meeting"]["line"], work["evidence"][0]["note"])
        self.assertEqual(work["accepted"]["confirmed_by"], "issued")
        accepted = [a for a in self.audits() if a[0] == "floor.accepted"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual((accepted[0][2]["cid"], accepted[0][2]["work"]), (cid, work["id"]))
        self.assertNotIn(PROMISE, json.dumps(self.audits()))                   # never text in audit

    def test_idempotent_per_meeting_and_cid(self):
        cid, _ = self.promise_meeting()
        with Store(self.home) as store:
            first = store.floor_accept("m1", cid, "Send review notes")
            again = store.floor_accept("m1", cid, "Another title")
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["accepted"]["repeated"])
        self.assertEqual(len(self.meeting_works()), 1)

    def test_k1_two_concurrent_callers_get_one_work(self):
        cid, _ = self.promise_meeting()
        barrier, results, errors = threading.Barrier(2), [], []

        def call():
            try:
                with Store(self.home) as store:
                    barrier.wait()
                    results.append(store.floor_accept("m1", cid, "Send review notes")["id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(self.meeting_works()), 1)
        self.assertEqual(len([a for a in self.audits() if a[0] == "floor.accepted"]), 1)

    def test_unknown_cid_refused_and_audited(self):
        self.promise_meeting()
        with Store(self.home) as store:
            with self.assertRaisesRegex(ValueError, "unknown_cid"):
                store.floor_accept("m1", "cid-0000000000000000", "x")
        refused = [a for a in self.audits() if a[0] == "floor.refused"]
        self.assertEqual(refused[-1][2]["code"], "unknown_cid")
        self.assertEqual(self.meeting_works(), [])

    def test_declined_cid(self):
        cid = self._meeting_with_confirm(False)
        with Store(self.home) as store:
            with self.assertRaisesRegex(ValueError, "declined"):
                store.floor_accept("m3", cid, "x")
        self.assertEqual([a[2]["code"] for a in self.audits() if a[0] == "floor.refused"], ["declined"])

    def test_confirmed_cid_records_panel_line(self):
        cid = self._meeting_with_confirm(True)
        with Store(self.home) as store:
            work = store.floor_accept("m3", cid, "x")
        self.assertTrue(work["accepted"]["confirmed_by"].startswith("human-panel line "))

    def _meeting_with_confirm(self, yes):
        """A meeting whose stdin waits for the answer's cid, then sends confirm{cid, yes}."""
        import queue
        lines = queue.Queue()

        class Feed(io.TextIOBase):
            def readline(self_inner):
                return lines.get(timeout=20)

        put = lambda obj: lines.put(json.dumps(dict(v=1, **obj)) + "\n")   # noqa: E731
        put(dict(type="begin", meeting="m3", goal="Weekly review"))
        put(dict(type="ask", id="a1", rev=0, question="PROMISE?"))

        class Out(io.StringIO):
            def write(self_inner, s):
                for raw in s.splitlines():
                    obj = json.loads(raw)
                    if obj.get("type") == "answer":
                        put(dict(type="confirm", cid=obj["commitments"][0]["cid"], yes=yes))
                        put(dict(type="end"))
                return super().write(s)

        stdout = Out()
        rc = floor.attach(self.home, self.key_file, stdin=Feed(), stdout=stdout, session_factory=PromiseSession)
        self.assertEqual(rc, 0)
        answer = [json.loads(l) for l in stdout.getvalue().splitlines() if '"answer"' in l][0]
        return answer["commitments"][0]["cid"]

    def test_meeting_running_without_end_line(self):
        fj.append(self.home, "m9", "begin", {})
        fj.append(self.home, "m9", "ask", dict(id="a1", rev=0, question="q"))
        cid = floormeeting.commit(self.home, "m9", "a1", PROMISE)
        with Store(self.home) as store:
            with self.assertRaisesRegex(ValueError, "meeting_running"):
                store.floor_accept("m9", cid, "x")

    def test_meeting_running_while_its_hold_is_held(self):
        cid, _ = self.promise_meeting()
        with Store(self.home) as store:
            store.meeting_begin("m1", "stand")
            try:
                with self.assertRaisesRegex(ValueError, "meeting_running"):
                    store.floor_accept("m1", cid, "x")
            finally:
                store.meeting_end("m1")

    def test_no_wrap_event(self):
        fj.append(self.home, "m8", "begin", {})
        fj.append(self.home, "m8", "ask", dict(id="a1", rev=0, question="q"))
        cid = floormeeting.commit(self.home, "m8", "a1", PROMISE)
        fj.append(self.home, "m8", "end", dict(reason="end"))
        with Store(self.home) as store:
            with self.assertRaisesRegex(ValueError, "no_wrap"):
                store.floor_accept("m8", cid, "x")

    def test_no_wrap_when_the_event_is_unauthenticated(self):
        cid, _ = self.promise_meeting(meeting="m7")
        with Store(self.home) as store:
            store.db.execute("UPDATE events SET data=json_set(data, '$.auth', NULL) WHERE data LIKE '%floor:m7:wrap%'")
            with self.assertRaisesRegex(ValueError, "no_wrap"):
                store.floor_accept("m7", cid, "x")

    def test_chain_broken_after_the_wrap(self):
        cid, _ = self.promise_meeting()
        fj.append(self.home, "m1", "floor_note", dict(text="written after the wrap"))
        with Store(self.home) as store:
            with self.assertRaisesRegex(ValueError, "chain_broken"):
                store.floor_accept("m1", cid, "x")

    def test_k2_existing_work_gets_evidence_never_a_new_work(self):
        with Store(self.home) as store:
            work = store.work_put(title="Review notes", status="waiting", wake_at="2099-01-01T00:00:00Z")["id"]
        cid, wrap = self.promise_meeting(question="PROMISE WORK=%s ok" % work)
        with Store(self.home) as store:
            out = store.floor_accept("m1", cid, "ignored title")
            stored = store.work_get(work)
        self.assertEqual(out["id"], work)
        self.assertEqual((stored["status"], stored["title"]), ("waiting", "Review notes"))
        self.assertEqual(stored["evidence"][-1]["ref"], wrap)
        self.assertEqual(self.meeting_works(), [])

    def test_k2_unknown_and_closed_work_refused(self):
        with Store(self.home) as store:
            closed = store.work_put(title="Old", status="cancelled", cancel_reason="dropped")["id"]
        cid, _ = self.promise_meeting(question="PROMISE WORK=work-%s ok" % ("0" * 32), meeting="m5")
        cid2, _ = self.promise_meeting(question="PROMISE WORK=%s ok" % closed, meeting="m6")
        with Store(self.home) as store:
            with self.assertRaisesRegex(ValueError, "unknown_work"):
                store.floor_accept("m5", cid, "x")
            with self.assertRaisesRegex(ValueError, "work_closed"):
                store.floor_accept("m6", cid2, "x")
            self.assertEqual(store.work_get(closed)["status"], "cancelled")
        self.assertEqual([a[2]["code"] for a in self.audits() if a[0] == "floor.refused"],
                         ["unknown_work", "work_closed"])


class Q1Origin(Base):
    def test_work_put_refuses_origin_meeting(self):
        with Store(self.home) as store:
            with self.assertRaisesRegex(ValueError, "floor_accept"):
                store.work_put(title="x", origin="meeting")
        spec = [t for t in mcp.TOOLS if t["name"] == "work_put"][0]
        self.assertEqual(spec["inputSchema"]["properties"]["origin"]["enum"], ["self", "assigned"])

    def test_meeting_origin_work_publishes(self):
        from synthetics import board
        from synthetics.projects import ProjectStore
        with Store(self.home) as store:
            me = store.identity()["id"]
        with ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics") as ps:
            ps.grant(me, "post", by="operator")
        cid, _ = self.promise_meeting()
        with Store(self.home) as store:
            work = store.floor_accept("m1", cid, "Send review notes")
            store.work_put(id=work["id"], publish="synthetics")
            self.assertEqual(board.publish_drain(store)["appended"], 1)
        with ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics") as ps:
            row = ps.state()["backlog"][me + "|" + work["id"]]
        self.assertEqual(row["origin"], "meeting")

    def test_lookout_board_marks_meeting_work(self):
        from synthetics import lookout
        import inspect
        self.assertIn("from a meeting", inspect.getsource(lookout))


class MeetingRunTools(Base):
    def test_meeting_run_never_sees_floor_accept_ordinary_run_does(self):
        with Store(self.home) as store:
            store.meeting_begin("m1", "stand")
            hold = store.meeting_attach("m1")
            run = store.meeting_run_begin("m1", hold["token"])
        server = self.server(run["id"])
        self.assertNotIn("floor_accept", [t["name"] for t in server.tools()])
        with self.assertRaisesRegex(ValueError, "Unknown tool"):
            server.call("floor_accept", {"meeting": "m1", "cid": "cid-1", "title": "x"})
        with Store(self.home) as store:
            store.run_finish(run["id"], "done")
            store.meeting_end("m1", token=hold["token"])
            ordinary = store.run_begin()["id"]
        self.assertIn("floor_accept", [t["name"] for t in self.server(ordinary).tools()])

    def test_c1_floor_accept_is_the_only_new_tool(self):
        names = [t["name"] for t in mcp.TOOLS]
        self.assertEqual(names.count("floor_accept"), 1)
        self.assertEqual(names[names.index("work_get") + 1], "floor_accept")
        self.assertIn("floor_accept", mcp.MUTATIONS)


class G2Verify(Base):
    def run_with(self, calls):
        run = self.wrap_run()
        server = self.server(run)
        out = [server.call(name, args) for name, args in calls]
        with Store(self.home) as store:
            store.run_finish(run, "wrap")
        return run, out

    def test_clean_wrap_and_k3_no_false_positive(self):
        cid, wrap = self.promise_meeting()
        run = self.wrap_run()
        server = self.server(run)
        work = server.call("floor_accept", {"meeting": "m1", "cid": cid, "title": "Send review notes"})
        server.call("work_put", {"id": work["id"], "note": "From meeting m1 (journal meetings/m1)."})
        with Store(self.home) as store:
            store.run_finish(run, "wrap")
        result = flooraccept.verify(self.home, run)
        self.assertEqual(result["verdict"], "clean", result)
        self.assertEqual(result["meetings"][0]["chain"], "chain_ok")
        self.assertEqual(result["meetings"][0]["accepted"], [cid])
        self.assertEqual(result["meetings"][0]["unaccepted"], [])

    def test_unattributed_work_and_room_correction_replaces(self):
        cid, wrap = self.promise_meeting()
        heard = [n for n, l in enumerate(fj.lines(self.home, "m1"), 1) if l["op"] == "heard"][0]
        with Store(self.home) as store:
            store.entity_put("synthetics", "Synthetics", "project")
            old = store.memory_put("synthetics", "deadline", "Friday", source="event-x")["id"]
        run, _ = self.run_with([
            ("work_put", {"title": "Move deadline", "note": "promised in meeting m1"}),
            ("work_put", {"title": "Other", "evidence": [{"level": "attempted", "ref": "receipt:x",
                                                           "note": "see %s" % wrap[:14]}]}),
            ("memory_put", {"subject": "synthetics", "predicate": "deadline", "value": "Monday",
                            "source": "meeting m1 journal line %d" % heard, "replaces": old}),
        ])
        result = flooraccept.verify(self.home, run)
        self.assertEqual(result["verdict"], "violations")
        kinds = [v["kind"] for v in result["violations"]]
        self.assertEqual(kinds, ["unattributed_meeting_work", "unattributed_meeting_work", "room_correction_replaces"])
        self.assertEqual(result["violations"][2]["lines"], [heard])
        self.assertEqual(result["meetings"][0]["unaccepted"], [cid])

    def test_token_match_is_exact(self):
        self.promise_meeting()
        run, _ = self.run_with([("work_put", {"title": "m10 and xm1 and m1x are other things"})])
        self.assertEqual(flooraccept.verify(self.home, run)["verdict"], "clean")

    def test_q2_exemption_needs_a_real_human_answer(self):
        self.promise_meeting()
        q_ok = self._question("answered", "david", {"channel": "fieldpost"})
        q_proxy = self._question("answered_by_proxy", "david", {"channel": "fieldpost"})
        q_other = self._question("answered", "rowan", {"channel": "fieldpost"})
        q_noauth = self._question("answered", "david", None)
        run, _ = self.run_with([
            ("work_put", {"title": "Ok from m1", "evidence": [{"level": "attempted", "ref": q_ok}]}),
            ("work_put", {"title": "Proxy from m1", "evidence": [{"level": "attempted", "ref": q_proxy}]}),
            ("work_put", {"title": "Other from m1", "evidence": [{"level": "attempted", "ref": q_other}]}),
            ("work_put", {"title": "Noauth from m1", "evidence": [{"level": "attempted", "ref": q_noauth}]}),
        ])
        result = flooraccept.verify(self.home, run)
        self.assertEqual([v["receipt_line"] for v in result["violations"]], [2, 3, 4])

    def _question(self, status, to, auth, created_at=None):
        import uuid
        from synthetics.capture import write_humans
        write_humans(self.home, {"default_human": "david", "senders": ["david"]})
        qid, eid = "question-" + uuid.uuid4().hex, "event-" + uuid.uuid4().hex
        with Store(self.home) as store, store._tx():
            store.db.execute("INSERT INTO events(id, data, processed_run) VALUES (?,?,?)",
                             (eid, json.dumps(dict(id=eid, sender=to, body="yes", auth=auth, kind="message")), "x"))
            store._save("questions", dict(id=qid, to=to, to_id=to, status=status, answer_event_id=eid, body="ok?",
                                          created_at=created_at or timestamp()), "question.test")
        return qid

    def test_no_evidence_without_a_wrap_event(self):
        with Store(self.home) as store:
            run = store.run_begin()["id"]
            store.run_finish(run, "x")
        self.assertEqual(flooraccept.verify(self.home, run)["verdict"], "no_evidence")
        self.assertEqual(flooraccept.verify(self.home, "run-nope")["verdict"], "no_evidence")


class G4BusyNit(Base):
    def busy(self, **patches):
        with Store(self.home) as store:
            store.run_begin()                                         # never recorded as ended
        stdin = io.StringIO(json.dumps(dict(v=1, type="begin", meeting="m-busy", grace=0)) + "\n")
        stdout = io.StringIO()
        with mock.patch.object(floor, "WAIT_EXTRA", 0.3), mock.patch.object(floor, "WAIT_POLL", 0.05), \
                mock.patch.object(Store, "meeting_end", side_effect=RuntimeError("db gone")), \
                mock.patch.multiple(Store, **patches) if patches else mock.patch.object(floor, "VERSION", 1):
            rc = floor.attach(self.home, self.key_file, stdin=stdin, stdout=stdout)
        return rc, [json.loads(l) for l in stdout.getvalue().splitlines()]

    def test_busy_line_emitted_when_meeting_end_raises(self):
        rc, out = self.busy()
        self.assertEqual(rc, 2)
        self.assertEqual(out[-1]["code"], "busy")
        failed = [a for a in self.audits() if a[0] == "floor.ended_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][2], {"reason": "busy", "error": "RuntimeError"})

    def test_k4_busy_line_emitted_even_when_the_audit_raises(self):
        rc, out = self.busy(audit_note=mock.Mock(side_effect=RuntimeError("audit gone")))
        self.assertEqual(rc, 2)
        self.assertEqual(out[-1]["code"], "busy")


class VerifyFence(_warrant.WarrantTest):
    """floor verify is fenced exactly like doctor / routine verify; refusals audited."""

    def test_session_other_home_refused_own_home_and_owner_allowed(self):
        self.refused("own_home_only", "rowan", "floor", "verify", "run-x", session="vesper")
        rows = [r for r in self.audit("vesper", "authority.refused") if "floor verify" in json.dumps(r)]
        self.assertTrue(rows)
        code, verdict, err = self.cli("vesper", "floor", "verify", self.runs["vesper"], session="vesper")
        self.assertEqual(code, 0, err)
        self.assertEqual(verdict["verdict"], "no_evidence")
        self.ok("rowan", "floor", "verify", "run-x")


for _name in dir(_warrant.WarrantTest):  # only VerifyFence's own test, not every inherited WarrantTest test again
    if _name.startswith("test_") and _name not in VerifyFence.__dict__:
        setattr(VerifyFence, _name, None)


if __name__ == "__main__":
    unittest.main()
