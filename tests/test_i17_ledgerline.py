"""Issue #17 Ledgerline E1: receipt result facts, hash chain, anchored head (C4: never alters the result)."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from synthetics import mcp
from synthetics.mcp import Server
from synthetics.store import Store

try:  # fail-first on main: the module doesn't exist there, so each test fails on its own
    from synthetics import ledgerline
except ImportError:  # pragma: no cover
    ledgerline = None


def sha(data):
    return hashlib.sha256(data).hexdigest()


class E1Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "employee"
        self.store = Store(self.home)
        self.store.initialize("Morgan", "Noteling maintainer")
        self.run = self.store.run_begin()
        self.bound = Store(self.home, run_id=self.run["id"])
        self.server = Server(self.bound, run_id=self.run["id"])
        self.path = self.home / "runs" / (self.run["id"] + ".mcp.jsonl")

    def tearDown(self):
        self.bound.close()
        self.store.close()
        self.temporary.cleanup()

    def respond(self, server, name, arguments):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        return server.respond(message)["result"]

    def receipts(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]


class ResultFacts(E1Case):
    def test_result_bytes_and_sha_equal_the_sent_text(self):
        event = self.store.feed("priya", "The original answer, é.")
        result = self.respond(self.server, "event_get", {"id": event["id"]})
        text = result["content"][0]["text"]
        receipt = self.receipts()[-1]
        self.assertEqual(receipt["result"]["bytes"], len(text.encode("utf-8")))
        self.assertEqual(receipt["result"]["sha256"], sha(text.encode("utf-8")))
        self.assertEqual(receipt["result"]["id"], event["id"])
        self.assertEqual(receipt["result"]["body_len"], len("The original answer, é."))
        self.assertEqual(receipt["result"]["body_offset"], 0)
        self.assertNotIn("original answer", self.path.read_text(encoding="utf-8"))

    def test_event_get_chunk_facts(self):
        event = self.store.feed("priya", "x" * 100)
        self.respond(self.server, "event_get", {"id": event["id"], "body_offset": 40, "body_chars": 25})
        facts = self.receipts()[-1]["result"]
        self.assertEqual((facts["body_offset"], facts["returned_chars"], facts["body_len"]), (40, 25, 100))

    def test_since_page_summary_has_ids_timestamps_and_cursor(self):
        events = [self.store.feed("priya", "body %d" % n) for n in range(3)]
        self.respond(self.server, "event_search", {"since": "1970-01-01T00:00:00Z", "limit": 2})
        facts = self.receipts()[-1]["result"]
        self.assertEqual(facts["ids"], [events[0]["id"], events[1]["id"]])
        self.assertEqual(facts["count"], 2)
        self.assertTrue(facts["capped"])
        self.assertEqual(facts["created_first"], events[0]["created_at"])
        self.assertEqual(facts["created_last"], events[1]["created_at"])
        self.assertEqual(facts["unread_from"], events[2]["created_at"])
        self.assertIsNotNone(facts["next_cursor"])
        self.assertNotIn("body 0", self.path.read_text(encoding="utf-8"))

    def test_query_page_lists_truncated_ids_with_body_len(self):
        event = self.store.feed("priya", "needle " + "y" * 2000)
        self.respond(self.server, "event_search", {"query": "needle"})
        facts = self.receipts()[-1]["result"]
        self.assertEqual(facts["ids"], [event["id"]])
        self.assertEqual(facts["truncated"], [{"id": event["id"], "created_at": event["created_at"],
                                               "body_len": len("needle " + "y" * 2000)}])
        self.assertEqual(facts["truncated_from"], event["created_at"])

    def test_memory_search_summary(self):
        self.store.entity_put("proj", "Proj", "project")
        memory = self.store.memory_put(subject="proj", predicate="fact", value="secret value text", source="test")
        self.respond(self.server, "memory_search", {"since": "1970-01-01T00:00:00Z"})
        facts = self.receipts()[-1]["result"]
        self.assertIn(memory["id"], facts["ids"])
        self.assertFalse(facts["capped"])
        self.assertNotIn("secret value text", self.path.read_text(encoding="utf-8"))

    def test_failed_call_has_error_and_no_result(self):
        self.respond(self.server, "event_get", {"id": "event-" + "0" * 32})
        receipt = self.receipts()[-1]
        self.assertFalse(receipt["success"])
        self.assertNotIn("result", receipt)
        self.assertIn("prev_sha256", receipt)

    def test_private_tools_stay_redacted(self):
        self.respond(self.server, "self_note", {"section": "how-i-work", "text": "private words here",
                                                "source": "s", "key": "k1"})
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("private words here", raw)
        self.assertIn("result", self.receipts()[-1])


class Chain(E1Case):
    def test_prev_sha256_links_each_line_and_run_finish_anchors_head(self):
        event = self.store.feed("priya", "a")
        for _ in range(3):
            self.respond(self.server, "event_get", {"id": event["id"]})
        lines = [line.encode("utf-8") for line in self.path.read_text(encoding="utf-8").splitlines()]
        records = [json.loads(line) for line in lines]
        self.assertIsNone(records[0]["prev_sha256"])
        self.assertEqual(records[1]["prev_sha256"], sha(lines[0]))
        self.assertEqual(records[2]["prev_sha256"], sha(lines[1]))
        finished = self.store.run_finish(self.run["id"], "done")
        self.assertEqual(finished["receipt_chain"], {"head_sha256": sha(lines[2]), "lines": 3})
        audit = self.store.db.execute("SELECT data FROM audit WHERE action='run.finished' ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(audit["data"])["receipt_chain"]["lines"], 3)
        self.assertEqual(ledgerline.check_chain(self.path, finished["receipt_chain"])["chain"], "chain_ok")

    def test_edit_breaks_chain_and_missing_anchor_is_unanchored(self):
        event = self.store.feed("priya", "a")
        for _ in range(2):
            self.respond(self.server, "event_get", {"id": event["id"]})
        self.assertEqual(ledgerline.check_chain(self.path, None)["chain"], "unanchored")
        finished = self.store.run_finish(self.run["id"], "done")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        edited = json.loads(lines[0])
        edited["success"] = False
        self.path.write_text(json.dumps(edited, ensure_ascii=False) + "\n" + lines[1] + "\n", encoding="utf-8")
        self.assertEqual(ledgerline.check_chain(self.path, finished["receipt_chain"])["chain"], "chain_broken")
        # Rewriting the whole chain consistently is caught by the anchor outside runs/.
        self.path.write_text(json.dumps(dict(json.loads(lines[0]), prev_sha256=None)) + "\n", encoding="utf-8")
        self.assertEqual(ledgerline.check_chain(self.path, finished["receipt_chain"])["chain"], "chain_broken")

    def test_run_without_tool_calls_anchors_zero_lines_and_fail_and_interrupt_anchor(self):
        finished = self.store.run_finish(self.run["id"], "quiet")
        self.assertEqual(finished["receipt_chain"], {"head_sha256": None, "lines": 0})
        failed = self.store.run_fail(self.store.run_begin()["id"], "boom")
        self.assertEqual(failed["receipt_chain"], {"head_sha256": None, "lines": 0})
        self.store.run_begin()
        self.store.recover_runs()
        interrupted = [r for r in self.store.run_list(10) if r["status"] == "interrupted"]
        self.assertEqual(interrupted[0]["receipt_chain"], {"head_sha256": None, "lines": 0})


class NeverAltersResult(E1Case):
    """C4: an E1 receipt write or hash failure never fails or alters the tool result."""

    def outputs(self, server):
        event_ids = [e["id"] for e in self.store.event_search(limit=5)]
        calls = [("event_get", {"id": event_ids[0]}), ("event_search", {"query": "alpha"}),
                 ("event_search", {"since": "1970-01-01T00:00:00Z", "limit": 1}),
                 ("work_put", {"title": "t", "key": "w-%d" % id(server)})]
        out = []
        for name, arguments in calls:
            text = json.dumps(self.respond(server, name, arguments), ensure_ascii=False, sort_keys=True)
            if name == "work_put":
                text = None  # new IDs differ per call; checked for success below
            out.append(text)
        return out

    def test_result_bytes_identical_with_e1_on_off_and_failing(self):
        self.store.feed("priya", "alpha one")
        self.store.feed("priya", "alpha two " + "z" * 900)
        off = Server(self.store)  # run_id None: no receipt at all
        baseline = self.outputs(off)
        on = self.outputs(self.server)
        self.assertEqual(on, baseline)
        with mock.patch.object(ledgerline, "result_facts", side_effect=RuntimeError("hash failed")):
            failing = self.outputs(self.server)
        self.assertEqual(failing, baseline)
        self.assertEqual(self.receipts()[-1]["result_error"], "RuntimeError")
        with mock.patch.object(ledgerline, "last_line", side_effect=RuntimeError("tail failed")):
            failing_write = self.outputs(self.server)
        self.assertEqual(failing_write, baseline)
        with mock.patch.object(mcp.os, "fsync", side_effect=OSError("disk")):
            self.assertEqual(self.outputs(self.server), baseline)


if __name__ == "__main__":
    unittest.main()
