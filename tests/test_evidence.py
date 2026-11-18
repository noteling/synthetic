"""M14 (SG-06): work completion needs evidence the store can check."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from synthetics.cli import main
from synthetics.mcp import Server
from synthetics.store import Store


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "employee"
        self.store = Store(self.home)
        self.me = self.store.initialize("Rowan", "Noteling maintainer")["id"]
        self.artifact = Path(self.tmp.name) / "artifact.txt"
        self.artifact.write_text("built\n")
        # M22: verified needs a channel-authenticated input.
        self.key = b"k" * 32
        self.store.channel_register("test-chan", Store.credential_hash(self.key), ["manual", "telegram:"], "test")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def done(self, store, work_id, *evidence):
        return store.work_put(id=work_id, status="done", evidence=[dict(level=l, ref=r) for l, r in evidence])

    # --- the completion rule -------------------------------------------------
    def test_bare_done_and_attempted_only_are_rejected(self):
        work = self.store.work_put("Ship")
        with self.assertRaisesRegex(ValueError, "Completing work needs evidence"):
            self.store.work_put(id=work["id"], status="done")
        message = self.store.message_send("david", "Shipped it", key="m1")
        with self.assertRaisesRegex(ValueError, "attempted alone"):
            self.done(self.store, work["id"], ("attempted", message["id"]))
        self.assertEqual(self.store.work_get(work["id"])["status"], "ready")

    def test_file_evidence_records_hash_and_size(self):
        work = self.store.work_put("Ship")
        closed = self.done(self.store, work["id"], ("observed", str(self.artifact)))
        item = closed["evidence"][0]
        self.assertEqual(closed["status"], "done")
        self.assertEqual(item["ref_kind"], "file")
        self.assertEqual(item["check"]["size"], 6)
        self.assertEqual(len(item["check"]["sha256"]), 64)
        with self.assertRaisesRegex(ValueError, "at most observed"):
            self.done(self.store, self.store.work_put("Other")["id"], ("verified", str(self.artifact)))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.done(self.store, self.store.work_put("Other")["id"], ("observed", str(self.artifact) + ".missing"))

    def test_own_run_transcripts_are_not_evidence(self):
        runs = self.home / "runs"
        runs.mkdir(exist_ok=True)
        transcript = runs / "run-x.claude.json"
        transcript.write_text("{}")
        work = self.store.work_put("Ship")
        with self.assertRaisesRegex(ValueError, "Own session transcripts"):
            self.done(self.store, work["id"], ("observed", str(transcript)))

    def test_event_level_depends_on_sender(self):
        colleague = self.store.feed("david", "Looks right to me", credential=self.key)
        own = self.store.feed(self.me, "note to self")
        system = self.store.feed("source-watcher", "doc changed", kind="source_update")
        work = self.store.work_put("Ship")
        with self.assertRaisesRegex(ValueError, "at most observed"):
            self.done(self.store, work["id"], ("verified", own["id"]))
        with self.assertRaisesRegex(ValueError, "at most observed"):
            self.done(self.store, work["id"], ("verified", system["id"]))
        closed = self.done(self.store, work["id"], ("verified", colleague["id"]))
        self.assertEqual(closed["evidence"][0]["check"]["sender"], "david")
        self.assertEqual(closed["evidence"][0]["check"]["auth"], "test-chan")

    def test_self_authored_refs_capped_at_attempted(self):
        self.store.entity_put("david", "David", "person")
        memory = self.store.memory_put("david", "said", "fine", source="chat")
        message = self.store.message_send("david", "Done", key="m2")
        other = self.store.work_put("Other")
        work = self.store.work_put("Ship")
        for ref in (memory["id"], message["id"], other["id"]):
            with self.assertRaisesRegex(ValueError, "at most attempted"):
                self.done(self.store, work["id"], ("observed", ref))
        with self.assertRaisesRegex(ValueError, "its own evidence"):
            self.done(self.store, work["id"], ("attempted", work["id"]))

    def test_question_verifies_only_once_answered(self):
        work = self.store.work_put("Ship")
        question = self.store.question_ask("david", "Does it work for you?", work["id"], key="q1")
        with self.assertRaisesRegex(ValueError, "at most attempted"):
            self.done(self.store, work["id"], ("verified", question["id"]))
        self.store.feed("david", "Yes", reply_to=question["id"], credential=self.key)
        closed = self.done(self.store, work["id"], ("verified", question["id"]))
        self.assertEqual(closed["evidence"][0]["check"]["status"], "answered")

    def test_runs_current_rejected_finished_observed(self):
        earlier = self.store.run_begin()
        self.store.run_finish(earlier["id"], "built it")
        current = self.store.run_begin()
        work = self.store.work_put("Ship")
        with Store(self.home, run_id=current["id"]) as worker:
            with self.assertRaisesRegex(ValueError, "current run"):
                self.done(worker, work["id"], ("observed", current["id"]))
            closed = self.done(worker, work["id"], ("observed", earlier["id"]))
        self.assertEqual(closed["evidence"][0]["recorded_by_run"], current["id"])

    def test_external_refs_and_unknown_refs(self):
        work = self.store.work_put("Ship")
        with self.assertRaisesRegex(ValueError, "Evidence ref must be"):
            self.done(self.store, work["id"], ("observed", "trust me"))
        with self.assertRaisesRegex(ValueError, "Evidence ref must be"):
            self.done(self.store, work["id"], ("observed", "receipt:"))
        with self.assertRaises(ValueError):
            self.done(self.store, work["id"], ("observed", "event-doesnotexist"))
        # M22 N2: free-form receipts are citable but capped at attempted.
        for ref in ("receipt:telegram:123", "https://example.com/x"):
            with self.assertRaisesRegex(ValueError, "at most attempted"):
                self.done(self.store, work["id"], ("observed", ref))
        closed = self.done(self.store, work["id"], ("attempted", "receipt:telegram:123"),
                           ("observed", str(self.artifact)))
        self.assertEqual(closed["evidence"][0]["ref_kind"], "external")

    def test_reopened_work_needs_fresh_evidence(self):
        work = self.store.work_put("Ship")
        self.done(self.store, work["id"], ("observed", str(self.artifact)))
        reopened = self.store.work_put(id=work["id"], status="ready", note="regressed")
        self.assertEqual(reopened["evidence_floor"], 1)
        with self.assertRaisesRegex(ValueError, "Completing work needs evidence"):
            self.store.work_put(id=work["id"], status="done")
        closed = self.done(self.store, work["id"], ("observed", str(self.artifact)))
        self.assertEqual(len(closed["evidence"]), 2)

    def test_cancel_needs_reason(self):
        work = self.store.work_put("Ship")
        with self.assertRaisesRegex(ValueError, "cancel_reason"):
            self.store.work_put(id=work["id"], status="cancelled")
        with self.assertRaisesRegex(ValueError, "cancel_reason"):
            self.store.work_put(id=work["id"], status="cancelled", cancel_reason="  ")
        cancelled = self.store.work_put(id=work["id"], status="cancelled", cancel_reason="superseded by M16")
        self.assertEqual(cancelled["cancellation"]["reason"], "superseded by M16")

    def test_override_operator_only(self):
        work = self.store.work_put("Cleanup")
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as worker:
            with self.assertRaisesRegex(ValueError, "operator action"):
                worker.work_put(id=work["id"], status="done", override="nothing to cite")
        closed = self.store.work_put(id=work["id"], status="done", override="nothing to cite")
        self.assertEqual(closed["evidence"][-1]["level"], "operator-override")
        self.assertIsNone(closed["evidence"][-1]["ref"])

    def test_invalid_items_rejected(self):
        work = self.store.work_put("Ship")
        for bad in ("x", dict(level="certain", ref=str(self.artifact)),
                    dict(level="observed", ref=str(self.artifact), extra=1),
                    dict(level="observed", ref=str(self.artifact), note=5)):
            with self.assertRaises(ValueError):
                self.store.work_put(id=work["id"], status="done", evidence=[bad])
        with self.assertRaises(ValueError):
            self.store.work_put(id=work["id"], status="done", evidence={"level": "observed"})

    # --- migration ------------------------------------------------------------
    def test_v1_home_marks_finished_work_legacy(self):
        finished = self.store.work_put("Old", status="done", override="fixture")
        open_work = self.store.work_put("Open")
        self.store.close()
        db = sqlite3.connect(self.home / "state.sqlite3")
        row = json.loads(db.execute("SELECT data FROM works WHERE id=?", (finished["id"],)).fetchone()[0])
        row.pop("evidence", None)
        db.execute("UPDATE works SET data=? WHERE id=?", (json.dumps(row), finished["id"]))
        db.execute("PRAGMA user_version=1")
        db.commit()
        db.close()
        self.store = Store(self.home)
        self.assertTrue(self.store.work_get(finished["id"])["legacy"])
        self.assertNotIn("legacy", self.store.work_get(open_work["id"]))
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], Store.SCHEMA_VERSION)

    def test_rerun_after_old_store_reset_keeps_evidence_closures(self):
        # B1: a pre-M14 Store sets user_version=1 on every open, so the v2
        # migration reruns on a home that already has M14 closures.
        evidenced = self.store.work_put("Evidenced")
        self.done(self.store, evidenced["id"], ("observed", str(self.artifact)))
        overridden = self.store.work_put("Overridden", status="done", override="operator fixture")
        cancelled = self.store.work_put("Dropped")
        self.store.work_put(id=cancelled["id"], status="cancelled", cancel_reason="superseded")
        plain = self.store.work_put("Plain old", status="done", override="fixture")
        self.store.close()
        db = sqlite3.connect(self.home / "state.sqlite3")
        row = json.loads(db.execute("SELECT data FROM works WHERE id=?", (plain["id"],)).fetchone()[0])
        row.pop("evidence", None)
        db.execute("UPDATE works SET data=? WHERE id=?", (json.dumps(row), plain["id"]))
        db.execute("PRAGMA user_version=1")
        db.commit()
        db.close()
        self.store = Store(self.home)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], Store.SCHEMA_VERSION)
        for kept in (evidenced, overridden, cancelled):
            self.assertNotIn("legacy", self.store.work_get(kept["id"]))
        self.assertTrue(self.store.work_get(plain["id"])["legacy"])

    def test_newer_home_is_not_downgraded(self):
        self.store.close()
        db = sqlite3.connect(self.home / "state.sqlite3")
        db.execute("PRAGMA user_version=%d" % (Store.SCHEMA_VERSION + 1))
        db.commit()
        db.close()
        self.store = Store(self.home)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], Store.SCHEMA_VERSION + 1)

    # --- interfaces -----------------------------------------------------------
    def test_mcp_exposes_evidence_but_not_override(self):
        server = Server(self.store)

        def call(arguments):
            return server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "work_put", "arguments": arguments}})["result"]

        work = json.loads(call({"title": "Ship"})["content"][0]["text"])
        self.assertTrue(call({"id": work["id"], "status": "done"})["isError"])
        self.assertTrue(call({"id": work["id"], "status": "done", "override": "x"})["isError"])
        self.assertTrue(call({"id": work["id"], "status": "done",
                              "evidence": [{"level": "observed", "ref": str(self.artifact), "bogus": 1}]})["isError"])
        self.assertTrue(call({"id": work["id"], "status": "done", "evidence": [{"level": "sure", "ref": "x"}]})["isError"])
        ok = call({"id": work["id"], "status": "done",
                   "evidence": [{"level": "observed", "ref": str(self.artifact), "note": "built"}]})
        self.assertFalse(ok.get("isError", False), ok)
        self.assertEqual(self.store.work_get(work["id"])["status"], "done")

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["--home", str(self.home)] + list(args))
            except SystemExit as stop:
                code = stop.code
        return code or 0, out.getvalue() + err.getvalue()

    def test_cli_evidence_cancel_and_override(self):
        work = self.store.work_put("Ship")
        other = self.store.work_put("Drop")
        cleanup = self.store.work_put("Cleanup")
        self.assertNotEqual(self.cli("work", "update", work["id"], "--status", "done")[0], 0)
        self.assertEqual(self.cli("work", "update", work["id"], "--status", "done",
                                  "--evidence", "observed", str(self.artifact))[0], 0)
        self.assertNotEqual(self.cli("work", "update", other["id"], "--status", "cancelled")[0], 0)
        self.assertEqual(self.cli("work", "update", other["id"], "--status", "cancelled",
                                  "--cancel-reason", "duplicate")[0], 0)
        self.assertEqual(self.cli("work", "update", cleanup["id"], "--status", "done",
                                  "--override", "no artifact")[0], 0)
        self.assertEqual(self.store.work_get(work["id"])["status"], "done")
        self.assertEqual(self.store.work_get(other["id"])["cancellation"]["reason"], "duplicate")
        self.assertEqual(self.store.work_get(cleanup["id"])["evidence"][-1]["level"], "operator-override")


if __name__ == "__main__":
    unittest.main()
