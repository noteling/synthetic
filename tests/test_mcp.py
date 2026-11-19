import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from synthetics.mcp import Server, TOOLS, serve
from synthetics.store import Store


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "employee"
        self.store = Store(self.home)
        self.identity = self.store.initialize("Morgan", "Noteling maintainer")
        self.server = Server(self.store)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def request(self, method, params=None):
        return self.server.respond({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def call(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})["result"]

    def test_initialize_list_and_notifications(self):
        result = self.request("initialize", {"protocolVersion": "2025-03-26"})["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])
        tools = self.request("tools/list")["result"]["tools"]
        # RW-1 C1: screen_glance is offered only in a home that opts in (glance.json); this one doesn't. The same
        # for the stage-2 click tools (click.json).
        self.assertEqual(tools, [entry for entry in TOOLS
                                 if entry["name"] not in ("screen_glance", "screen_click_request", "screen_click")])
        self.assertIn("memory_search", {entry["name"] for entry in tools})
        self.assertIsNone(self.server.respond({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_memory_is_durable_and_unknown_input_cannot_call_arbitrary_methods(self):
        self.call("entity_put", {"id": "priya", "name": "Priya", "kind": "person"})
        result = self.call("memory_put", {"subject": "priya", "predicate": "owns", "value": "release coordination", "source": "onboarding event"})
        self.assertFalse(result.get("isError", False), result)
        memories = json.loads(self.call("memory_search", {"query": "release"})["content"][0]["text"])["memories"]
        self.assertEqual(memories[0]["value"], "release coordination")
        rejected = self.call("run_finish", {"run_id": "arbitrary"})
        self.assertTrue(rejected["isError"])
        invalid = self.call("entity_put", {"id": "x", "name": "X", "kind": "person", "unexpected": "value"})
        self.assertTrue(invalid["isError"])

    def test_partial_work_update_does_not_reset_omitted_fields(self):
        created = self.call("work_put", {"title": "Investigate", "description": "Find the fault", "priority": 9, "note": "Inspect the fixture"})
        work = json.loads(created["content"][0]["text"])
        updated = self.call("work_put", {"id": work["id"], "note": "Fixture reproduced it"})
        value = json.loads(updated["content"][0]["text"])
        self.assertEqual(value["description"], "Find the fault")
        self.assertEqual(value["priority"], 9)
        self.assertEqual(value["title"], "Investigate")

    def test_processed_source_event_remains_retrievable(self):
        event = self.store.feed("priya", "The company calls the release checklist a green sheet.", thread="kt")
        run = self.store.run_begin()
        self.store.run_finish(run["id"], "Remembered the company term.")
        self.assertEqual(self.store.event_pending(), [])
        fetched = self.call("event_get", {"id": event["id"]})
        self.assertFalse(fetched.get("isError", False), fetched)
        source = json.loads(fetched["content"][0]["text"])
        self.assertEqual(source["sender"], "priya")
        self.assertIn("green sheet", source["body"])
        searched = self.call("event_search", {"query": "green sheet", "thread": "kt"})
        matches = json.loads(searched["content"][0]["text"])["events"]  # F2: {events, capped, truncated_from}
        self.assertEqual([match["id"] for match in matches], [event["id"]])

    def test_required_key_and_types_are_checked(self):
        self.assertTrue(self.call("question_ask", {"to": "david", "body": "Which behavior?"})["isError"])
        self.assertTrue(self.call("memory_search", {"limit": True})["isError"])
        self.assertTrue(self.call("memory_search", {"limit": 1000})["isError"])
        self.assertTrue(self.call("work_put", {"status": "invented"})["isError"])

    def test_recovered_run_cannot_mutate_through_bound_server(self):
        run = self.store.run_begin()
        bound = Store(self.home, run_id=run["id"])
        try:
            server = Server(bound, run_id=run["id"])
            server.call("entity_put", {"id": "before", "name": "Before", "kind": "concept"})
            self.store.recover_runs()
            with self.assertRaises(ValueError):
                server.call("entity_put", {"id": "after", "name": "After", "kind": "concept"})
            self.assertEqual(self.store.entities(query="After"), [])
        finally:
            bound.close()

    def test_bound_calls_record_success_and_rejection_without_full_results(self):
        event = self.store.feed("priya", "The original answer.")
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as bound:
            server = Server(bound, run_id=run["id"])
            server.call("event_get", {"id": event["id"]})
            with self.assertRaises(ValueError):
                server.call("event_get", {})
        path = self.home / "runs" / (run["id"] + ".mcp.jsonl")
        receipts = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0]["tool"], "event_get")
        self.assertEqual(receipts[0]["arguments"], {"id": event["id"]})
        self.assertTrue(receipts[0]["success"])
        self.assertIn("at", receipts[0])
        self.assertFalse(receipts[1]["success"])
        self.assertIn("error", receipts[1])
        self.assertNotIn("The original answer.", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_receipt_run_id_cannot_escape_log_directory(self):
        event = self.store.feed("priya", "An answer.")
        server = Server(self.store, run_id="../../escaped")
        server.call("event_get", {"id": event["id"]})
        self.assertFalse((self.home.parent / "escaped.mcp.jsonl").exists())
        self.assertFalse((self.home / "runs").exists())

    def test_stdio_recovers_from_invalid_json_and_emits_only_protocol(self):
        messages = "garbage\n" + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}) + "\n"
        output = io.StringIO()
        serve(self.home, input_stream=io.StringIO(messages), output_stream=output)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1], {"jsonrpc": "2.0", "id": 3, "result": {}})

    def test_real_subprocess_roundtrip(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "employee_context", "arguments": {}}},
        ]
        process = subprocess.run([sys.executable, "-m", "synthetics.mcp", "--home", str(self.home)],
                                 input="".join(json.dumps(item) + "\n" for item in requests), text=True,
                                 capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        context = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(context["identity"]["name"], "Morgan")


if __name__ == "__main__":
    unittest.main()
