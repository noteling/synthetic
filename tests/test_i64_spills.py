"""#64 slice 1: the two search tools refuse a max_bytes over 32000 through MCP; the stream reducer keeps the size the
client prints for a saved preview, only from the client's own line, and no path or text with it."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import ledgerline
from synthetics.mcp import TOOLS, Server
from synthetics.store import Store
from tests.test_e4_stream import MARKERS, PREVIEW, SPILL, stream_lines
from tests.test_i17_export_verify import HomeCase, run_cli

SEARCHES = {"event_search": "events", "memory_search": "memories"}


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class SearchMaximum(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / "a")
        self.s.initialize("A", "developer")
        self.s.entity_put("topic", "Topic", "project")
        for n in range(30):
            self.s.feed("someone", "green %d " % n + "x" * 4000, external_id="e%d" % n)
            self.s.memory_put("topic", "green %d" % n, "y" * 4000, source="file:x")
        self.server = Server(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def call(self, name, arguments):
        return self.server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})["result"]

    def test_over_the_maximum_is_refused_before_any_read(self):
        for name in SEARCHES:
            for base in ({"query": "green"}, {"since": "2000-01-01T00:00:00Z"}):
                for value in (32001, 48000, 120000):
                    with self.subTest(tool=name, max_bytes=value, **base):
                        with mock.patch.object(Store, name, side_effect=AssertionError("read")) as read:
                            result = self.call(name, dict(base, max_bytes=value, limit=200))
                        self.assertTrue(result.get("isError"), result)
                        self.assertIn("max_bytes is too large", result["content"][0]["text"])
                        read.assert_not_called()

    def test_the_maximum_itself_and_below_still_work(self):
        for name, key in SEARCHES.items():
            for value in (1024, 31999, 32000):
                with self.subTest(tool=name, max_bytes=value):
                    result = self.call(name, {"since": "2000-01-01T00:00:00Z", "limit": 200, "max_bytes": value})
                    self.assertFalse(result.get("isError", False), result)
                    self.assertTrue(json.loads(result["content"][0]["text"])[key])

    def test_default_equals_the_maximum(self):
        for name in SEARCHES:
            arguments = {"since": "2000-01-01T00:00:00Z", "limit": 200}
            self.assertEqual(self.call(name, arguments), self.call(name, dict(arguments, max_bytes=32000)))

    def test_offered_schema_states_the_maximum(self):
        for tool in TOOLS:
            if tool["name"] in SEARCHES:
                field = tool["inputSchema"]["properties"]["max_bytes"]
                self.assertEqual((field["minimum"], field.get("maximum")), (1024, 32000))
                self.assertIn("maximum 32000", field["description"])

    def test_store_still_takes_more(self):
        for name, key in SEARCHES.items():
            page = getattr(self.s, name)(since="2000-01-01T00:00:00Z", limit=200, max_bytes=120000)
            self.assertGreater(size(page[key]), 64000)


def preview(line, marker="<persisted-output>\n", tail="\n\nPreview (first 2KB):\nMARK-BODY\n...\n</persisted-output>"):
    return marker + line + tail


SAVED = ". Full output saved to: /Users/x/.claude/projects/MARK-SAVED/tool-results/b.txt"


class ReportedSize(unittest.TestCase):
    def fact(self, content, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-1.stream.jsonl"
            reducer = ledgerline.StreamReducer(path)
            reducer.feed(json.dumps({"type": "user", "message": {"content": [
                dict({"type": "tool_result", "tool_use_id": "toolu_1", "content": content}, **kw)]}}) + "\n")
            reducer.close()
            text = path.read_text()
        for marker in MARKERS:
            self.assertNotIn(marker, text)
        [fact] = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(fact["kind"], "displayed")
        return fact

    def test_the_clients_line_gives_the_size(self):
        for printed, number in (("58.6", 58.6), ("60", 60.0), ("9999.9", 9999.9)):
            with self.subTest(printed=printed):
                text = preview("Output too large (%sKB)" % printed + SAVED)
                for content in (text, [{"type": "text", "text": text}]):
                    fact = self.fact(content)
                    self.assertEqual((fact["shape"], fact["reported_kb"]), ("preview", number))
                    self.assertIs(fact["saved_outside_home"], True)
        self.assertEqual(self.fact(PREVIEW)["reported_kb"], 58.6)

    def test_only_the_number_is_kept(self):
        fact = self.fact(preview("Output too large (58.6KB)" + SAVED))
        self.assertEqual(set(fact), {"kind", "seq", "prev_sha256", "tool_use_id", "shape", "bytes", "sha256",
                                     "reported_kb", "saved_outside_home"})
        self.assertIsInstance(fact["reported_kb"], float)

    def test_the_commands_own_output_cannot_set_it(self):
        line = "Output too large (58.6KB)" + SAVED
        for label, text in (
                ("later line of the preview", preview("Saved" + SAVED, tail="\n\nPreview (first 2KB):\n" + line)),
                ("second marker inside the preview", preview("Saved" + SAVED, tail="\n<persisted-output>\n" + line)),
                ("not on the line after the marker", preview("\n" + line)),
                ("same line as the marker", preview(line, marker="<persisted-output> ")),
                ("something before it", preview("x" + line))):
            with self.subTest(label):
                fact = self.fact(text)
                self.assertEqual(fact["shape"], "preview")
                self.assertNotIn("reported_kb", fact)

    def test_other_forms_give_no_size(self):
        for printed in ("2MB", "58.6 KB", "58.65KB", "12345KB", "12345.6KB", ".5KB", "58.KB", "-1KB", "1e3KB",
                        "NaNKB", "58,6KB", "٥٨KB", "60000 bytes", "KB", ""):
            with self.subTest(printed=printed):
                fact = self.fact(preview("Output too large (%s)" % printed + SAVED))
                self.assertEqual(fact["shape"], "preview")
                self.assertNotIn("reported_kb", fact)
        fact = self.fact(preview("Output too large (58.6KB). Saved elsewhere"))
        self.assertNotIn("reported_kb", fact)

    def test_other_shapes_give_no_size(self):
        line = preview("Output too large (58.6KB)" + SAVED)
        for shape, content, kw in (("spilled", SPILL, {}), ("full", "out:\n" + line, {}),
                                   ("error", "exit 1\n" + line, {"is_error": True}),
                                   ("unknown", [{"type": "text", "text": line}, {"type": "image"}], {})):
            with self.subTest(shape):
                fact = self.fact(content, **kw)
                self.assertEqual(fact["shape"], shape)
                self.assertNotIn("reported_kb", fact)
        self.assertEqual(self.fact(SPILL)["reported_chars"], 96210)

    def test_the_key_is_allowed_and_never_a_forbidden_one(self):
        self.assertIn("reported_kb", ledgerline.STREAM_ALLOWED["displayed"])
        self.assertNotIn("reported_kb", ledgerline.STREAM_FORBIDDEN)


class Export(HomeCase):
    def test_export_passes_the_number(self):
        run = self.store.run_begin()
        folder = self.home / "runs"
        folder.mkdir(exist_ok=True)
        reducer = ledgerline.StreamReducer(folder / (run["id"] + ".stream.jsonl"))
        for line in stream_lines():
            reducer.feed(line)
        reducer.close()
        self.store.run_finish(run["id"], "ok")
        code, out, _ = run_cli("--home", str(self.home), "export", "runs", "--run", run["id"])
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.splitlines()]
        kbs = [line["facts"]["reported_kb"] for line in lines
               if line["kind"] == "stream" and "reported_kb" in line.get("facts", {})]
        self.assertEqual(kbs, [58.6])


if __name__ == "__main__":
    unittest.main()
