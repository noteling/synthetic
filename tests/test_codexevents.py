"""Codex JSONL completion and failure semantics; display records contain facts only."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from synthetics.codexevents import CodexReducer, MAX_LINE_BYTES, MAX_SUMMARY_BYTES


def event(kind, **values):
    return json.dumps(dict(type=kind, **values)) + "\n"


def message(text, kind="agent_message", **values):
    return event("item.completed", item=dict(type=kind, text=text, **values))


class CodexEvents(unittest.TestCase):
    def successful(self, reducer, text="Saved the next step."):
        reducer.feed(event("thread.started", thread_id="PRIVATE-THREAD"))
        reducer.feed(event("turn.started"))
        reducer.feed(message(text))
        reducer.feed(event("turn.completed", usage={"input_tokens": 12, "cached_input_tokens": 4,
                                                      "output_tokens": 7, "reasoning_output_tokens": 2}))
        return reducer.finish()

    def test_completion_returns_only_the_latest_completed_agent_message(self):
        reducer = CodexReducer()
        reducer.feed(message("Earlier progress."))
        reducer.feed(message("PRIVATE-REASONING", kind="reasoning"))
        reducer.feed(event("item.completed", item={"type": "command_execution",
                                                    "aggregated_output": "PRIVATE-TOOL-OUTPUT"}))
        reducer.feed(event("item.updated", item={"type": "agent_message", "text": "An incomplete update"}))
        reducer.feed(message("Final outcome."))
        reducer.feed(event("turn.completed"))
        result = reducer.finish()
        self.assertEqual(result["summary"], "Final outcome.")
        self.assertTrue(result["completed"])
        self.assertIsNone(result["error"])

    def test_records_token_usage_without_inventing_a_dollar_cost(self):
        result = self.successful(CodexReducer())
        self.assertEqual(result["usage"], {"input_tokens": 12, "cached_input_tokens": 4,
                                           "output_tokens": 7, "reasoning_output_tokens": 2})
        self.assertNotIn("cost", result)
        self.assertNotIn("total_cost_usd", result)

    def test_unrecognized_or_invalid_usage_fields_are_not_telemetry(self):
        reducer = CodexReducer()
        reducer.feed(message("Done"))
        reducer.feed(event("turn.completed", usage={"input_tokens": True, "output_tokens": -1,
                                                     "cached_input_tokens": 2.5, "reasoning_output_tokens": 3,
                                                     "total_cost_usd": 500, "secret": "PRIVATE-USAGE"}))
        self.assertEqual(reducer.finish()["usage"], {"reasoning_output_tokens": 3})

    def test_no_completion_marker_cannot_be_treated_as_success(self):
        reducer = CodexReducer()
        reducer.feed(message("A plausible final answer."))
        self.assertEqual(reducer.summary, "")
        result = reducer.finish()
        self.assertFalse(result["completed"])
        self.assertEqual(result["error_kind"], "incomplete")
        self.assertEqual(result["summary"], "")

    def test_empty_summary_is_distinct_from_incomplete(self):
        reducer = CodexReducer()
        reducer.feed(event("turn.completed"))
        result = reducer.finish()
        self.assertTrue(result["completed"])
        self.assertEqual(result["error_kind"], "empty")
        self.assertIn("summary", result["error"])

    def test_tool_text_is_not_a_summary(self):
        reducer = CodexReducer()
        reducer.feed(message("Looks like a result", kind="mcp_tool_call"))
        reducer.feed(event("turn.completed"))
        self.assertEqual(reducer.finish()["error_kind"], "empty")

    def test_terminal_error_wins_even_after_a_summary_and_completion(self):
        reducer = CodexReducer()
        reducer.feed(message("Saved the next step"))
        reducer.feed(event("turn.completed"))
        reducer.feed(event("turn.failed", error={"message": "Provider exploded PRIVATE-CREDENTIAL"}))
        result = reducer.finish()
        self.assertTrue(result["completed"])
        self.assertEqual(result["error_kind"], "provider")
        self.assertEqual(result["summary"], "")
        self.assertNotIn("PRIVATE-CREDENTIAL", json.dumps(result))

    def test_quota_and_authentication_errors_have_actionable_sanitized_categories(self):
        for kind, value, expected in (
            ("error", "You have exceeded your usage limit. PRIVATE-KEY", "quota"),
            ("turn.failed", {"message": "429 rate limit reached PRIVATE-KEY"}, "quota"),
            ("turn.failed", {"message": "Please log in: authentication token expired PRIVATE-KEY"}, "authentication"),
        ):
            with self.subTest(kind=kind, value=value):
                reducer = CodexReducer()
                reducer.feed(event(kind, **({"message": value} if kind == "error" else {"error": value})))
                result = reducer.finish()
                self.assertEqual(result["error_kind"], expected)
                self.assertNotIn("PRIVATE-KEY", json.dumps(result))

    def test_invalid_json_is_sticky_and_does_not_retain_the_raw_line(self):
        reducer = CodexReducer()
        reducer.feed("PRIVATE-MALFORMED\n")
        result = self.successful(reducer)
        self.assertEqual(result["error_kind"], "protocol")
        self.assertEqual(result["malformed_lines"], 1)
        self.assertEqual(result["summary"], "")
        self.assertNotIn("PRIVATE-MALFORMED", repr(vars(reducer)))

    def test_json_without_a_string_event_type_is_malformed(self):
        for value in ({"text": "PRIVATE"}, [], None, {"type": 12}):
            with self.subTest(value=value):
                reducer = CodexReducer()
                reducer.feed(json.dumps(value))
                self.assertEqual(reducer.finish()["malformed_lines"], 1)

    def test_unknown_event_type_is_forward_compatible(self):
        reducer = CodexReducer()
        reducer.feed(event("future.event", text="PRIVATE-FUTURE"))
        result = self.successful(reducer)
        self.assertIsNone(result["error"])
        self.assertNotIn("PRIVATE-FUTURE", repr(vars(reducer)))

    def test_invalid_item_shapes_are_protocol_errors_without_exceptions(self):
        for item in (None, [], {"type": []}, {"type": {}}, {"text": "PRIVATE"}):
            with self.subTest(item=item):
                reducer = CodexReducer()
                reducer.feed(event("item.completed", item=item))
                self.assertEqual(reducer.finish()["error_kind"], "protocol")

    def test_line_and_summary_bounds_fail_without_a_truncated_success(self):
        reducer = CodexReducer()
        reducer.feed("x" * (MAX_LINE_BYTES + 1))
        result = self.successful(reducer)
        self.assertEqual(result["oversized_lines"], 1)
        self.assertEqual(result["error_kind"], "protocol")
        reducer = CodexReducer()
        reducer.feed(message("x" * (MAX_SUMMARY_BYTES + 1)))
        reducer.feed(event("turn.completed"))
        result = reducer.finish()
        self.assertEqual(result["error_kind"], "protocol")
        self.assertEqual(result["summary"], "")

    def test_bounds_count_utf8_bytes_not_characters(self):
        reducer = CodexReducer()
        reducer.feed(message("\u00e9" * (MAX_SUMMARY_BYTES // 2 + 1)))
        reducer.feed(event("turn.completed"))
        self.assertEqual(reducer.finish()["error_kind"], "protocol")

    def test_bytes_input_and_blank_lines(self):
        reducer = CodexReducer()
        reducer.feed(b" \n")
        reducer.feed(message("Done").encode())
        reducer.feed(event("turn.completed").encode())
        self.assertEqual(reducer.finish()["summary"], "Done")

    def test_bad_utf8_is_a_sanitized_protocol_error(self):
        reducer = CodexReducer()
        reducer.feed(b"\xffPRIVATE-UTF8")
        result = reducer.finish()
        self.assertEqual(result["error_kind"], "protocol")
        self.assertEqual(result["malformed_lines"], 1)

    def test_finish_is_idempotent_and_freezes_late_events(self):
        reducer = CodexReducer()
        first = self.successful(reducer)
        reducer.feed(message("A late answer"))
        reducer.feed(event("error", message="late error"))
        self.assertEqual(reducer.finish(), first)

    def test_detached_reader_cannot_provide_late_success(self):
        reducer = CodexReducer()
        reducer.feed(message("Not committed yet"))
        reducer.detach(0.25)
        reducer.feed(event("turn.completed"))
        result = reducer.finish()
        self.assertEqual(result["error_kind"], "incomplete")
        self.assertFalse(result["completed"])

    def test_a_later_turn_cannot_reuse_an_old_summary(self):
        reducer = CodexReducer()
        reducer.feed(event("turn.started"))
        reducer.feed(message("First turn"))
        reducer.feed(event("turn.completed"))
        reducer.feed(event("turn.started"))
        reducer.feed(event("turn.completed"))
        self.assertEqual(reducer.finish()["error_kind"], "empty")

    def test_display_records_are_private_structural_facts_with_a_valid_chain(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run.stream.jsonl"
            reducer = CodexReducer(path)
            reducer.feed(event("item.started", item={"id": "PRIVATE-ID", "type": "mcp_tool_call",
                                                       "server": "PRIVATE-SERVER", "tool": "PRIVATE-TOOL",
                                                       "arguments": {"credential": "PRIVATE-ARG"}}))
            reducer.feed(event("item.updated", item={"type": "reasoning", "text": "PRIVATE-THINKING"}))
            reducer.feed(event("item.completed", item={"type": "command_execution", "command": "PRIVATE-CMD",
                                                         "aggregated_output": "PRIVATE-OUTPUT"}))
            self.successful(reducer, text="PRIVATE-SUMMARY")
            text = path.read_text()
            self.assertNotIn("PRIVATE", text)
            facts = [json.loads(line) for line in text.splitlines()]
            previous = None
            for index, (line, fact) in enumerate(zip(text.splitlines(), facts), 1):
                self.assertEqual(fact["seq"], index)
                self.assertEqual(fact["prev_sha256"], previous)
                self.assertEqual(fact["provider"], "codex")
                previous = hashlib.sha256(line.encode()).hexdigest()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("tool", [f["kind"] for f in facts])
            self.assertIn("turn", [f["kind"] for f in facts])

    def test_unwritable_fact_file_does_not_change_semantic_success(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.successful(CodexReducer(Path(folder) / "missing" / "stream.jsonl"))
            self.assertIsNone(result["error"])
            self.assertEqual(result["summary"], "Saved the next step.")


if __name__ == "__main__":
    unittest.main()
